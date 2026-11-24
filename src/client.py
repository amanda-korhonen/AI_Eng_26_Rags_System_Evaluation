"""
client.py - A small, polite client for the LiteratureExplorer RAG endpoint.

The endpoint has two routes:
    POST /v1/chat/completions   -> retrieval + generation (OpenAI-compatible)
    POST /v1/context            -> retrieval only (returns the relevant chunks)

The assignment sets rules for using the endpoint, and this client enforces them
so the other scripts (collect.py, system_tests.py, ...) don't have to:

    Rule from the assignment                      How this client handles it
    --------------------------------------------  ------------------------------------------
    Keys must not be hard-coded                   Loaded from .env via python-dotenv
    At most 2-3 requests in parallel              A semaphore caps concurrent requests (2)
    On 429/502 wait a few seconds, then retry     Exponential backoff with jitter, max 4 tries
    Stay below ~1,000 requests in total           Every real HTTP call is logged to disk and
                                                  counted; the client refuses to go over budget
    Cache responses, don't re-request             Successful responses are cached on disk,
                                                  keyed by route + request body
    Keep the raw responses (deliverable 4)        The cache *is* the raw responses (JSON files)

Usage (from another script):

    from client import RAGClient

    rag = RAGClient()
    res = rag.chat("What is WOLF?")
    print(res["answer"], res["latency_s"])

    ctx = rag.context("What is WOLF?")
    print(len(ctx["contexts"]), "chunks retrieved")

Quick smoke test from the command line:

    python src/client.py "What is WOLF?"
    python src/client.py --stats
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Project root = the folder above src/. Using absolute paths means the scripts
# work no matter which directory you run them from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Read RAG_BASE_URL and RAG_API_KEY (and the Lehmus settings) from .env.
load_dotenv(PROJECT_ROOT / ".env")

# Where raw responses and the request log are stored.
RAW_DIR = PROJECT_ROOT / "data" / "raw"
CACHE_DIR = RAW_DIR / "cache"            # one JSON file per unique request
REQUEST_LOG = RAW_DIR / "request_log.jsonl"  # one line per *real* HTTP call

# The assignment says "below about 1,000". We stop a bit earlier to leave
# headroom for re-running things at the end.
DEFAULT_BUDGET = 950

# HTTP status codes that mean "temporary problem, try again later".
# 429 = Too Many Requests, 502/503/504 = gateway/server temporarily unavailable.
RETRYABLE_STATUS = {429, 502, 503, 504}

ROUTE_CHAT = "/v1/chat/completions"
ROUTE_CONTEXT = "/v1/context"


class BudgetExceeded(RuntimeError):
    """Raised when sending another request would exceed the request budget."""


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #

class RAGClient:
    """Thread-safe client for the RAG endpoint with caching, retries and a budget.

    Parameters
    ----------
    max_parallel : how many requests may be in flight at once (assignment: 2-3).
    budget       : maximum number of real HTTP requests over the whole project.
    max_retries  : how many times to retry on 429/5xx or network errors.
    timeout_s    : how long to wait for one response before giving up.
    """

    def __init__(
        self,
        max_parallel: int = 2,
        budget: int = DEFAULT_BUDGET,
        max_retries: int = 4,
        timeout_s: float = 120.0,
    ) -> None:
        self.base_url = os.environ.get("RAG_BASE_URL", "").rstrip("/")
        self.api_key = os.environ.get("RAG_API_KEY", "")
        if not self.base_url or not self.api_key:
            raise RuntimeError(
                "RAG_BASE_URL and RAG_API_KEY must be set in .env (see README.md)."
            )

        self.budget = budget
        self.max_retries = max_retries

        # The semaphore is like a bouncer: only `max_parallel` threads can be
        # inside the "send request" block at the same time; others wait.
        self._semaphore = threading.Semaphore(max_parallel)
        # The lock protects the request counter and the log file, so two
        # threads can't both read "949" and both send request #950.
        self._lock = threading.Lock()

        # One shared HTTP connection pool (faster than opening a new one each time).
        self._http = httpx.Client(timeout=timeout_s)

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Count requests made in earlier runs, so the budget covers the whole project.
        self._request_count = self._count_logged_requests()

    # ------------------------------------------------------------------ #
    # Public, convenient methods
    # ------------------------------------------------------------------ #

    def chat(self, question: str, use_cache: bool = True) -> dict[str, Any]:
        """Retrieval + generation. Returns a normalised result dict:

        {
          "question":  the question sent,
          "answer":    the generated answer text (or None on error),
          "contexts":  retrieved chunk texts, if the endpoint includes them (else []),
          "sources":   per chunk: {"source": file name, "page": ..., "score": ...},
          "status":    HTTP status code (or None if the request never got through),
          "latency_s": seconds the (last) request took,
          "error":     error description or None,
          "from_cache": True if no request was sent,
          "raw":       the full JSON body, exactly as the endpoint returned it,
        }
        """
        payload = {"messages": [{"role": "user", "content": question}]}
        rec = self.request(ROUTE_CHAT, payload, use_cache=use_cache)

        body = rec.get("response") or {}
        answer = None
        try:
            # Standard OpenAI chat-completions shape: choices[0].message.content
            answer = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            pass

        return {
            "question": question,
            "answer": answer,
            "contexts": _extract_contexts(body),
            "sources": _extract_sources(body),
            "status": rec["status"],
            "latency_s": rec["latency_s"],
            "error": rec["error"],
            "from_cache": rec["from_cache"],
            "raw": body,
        }

    def context(self, query: str, use_cache: bool = True) -> dict[str, Any]:
        """Retrieval only. Returns the same shape as chat(), with answer=None."""
        rec = self.request(ROUTE_CONTEXT, {"query": query}, use_cache=use_cache)
        body = rec.get("response") or {}
        return {
            "question": query,
            "answer": None,
            "contexts": _extract_contexts(body),
            "sources": _extract_sources(body),
            "status": rec["status"],
            "latency_s": rec["latency_s"],
            "error": rec["error"],
            "from_cache": rec["from_cache"],
            "raw": body,
        }

    # ------------------------------------------------------------------ #
    # Low-level method (also used directly by system_tests.py for odd inputs)
    # ------------------------------------------------------------------ #

    def request(
        self,
        route: str,
        payload: Any,
        use_cache: bool = True,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Send `payload` to `route` and return a record with the full details.

        - `use_cache=False` forces a fresh request (e.g. for latency tests).
        - `api_key` overrides the key (e.g. to test that a wrong key is rejected).

        The returned record is also what gets saved to the cache file.
        """
        key = _cache_key(route, payload, api_key)
        cache_file = CACHE_DIR / f"{key}.json"

        # 1) Cache hit -> no network call, no budget used.
        if use_cache and cache_file.exists():
            rec = json.loads(cache_file.read_text(encoding="utf-8"))
            rec["from_cache"] = True
            return rec

        # 2) Cache miss -> send the request, with retries.
        rec = self._send_with_retries(route, payload, api_key)
        rec["from_cache"] = False

        # 3) Cache the result if it's a "final" answer. We cache successes
        #    (2xx) and client errors (4xx except 429), because sending the same
        #    request again would give the same result. We do NOT cache 429/5xx
        #    or network errors: those are temporary and worth retrying later.
        status = rec["status"]
        if status is not None and status < 500 and status != 429:
            cache_file.write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return rec

    @property
    def requests_used(self) -> int:
        """How many real HTTP requests have been sent so far (all runs)."""
        return self._request_count

    def close(self) -> None:
        self._http.close()

    # Support `with RAGClient() as rag: ...`
    def __enter__(self) -> "RAGClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _send_with_retries(
        self, route: str, payload: Any, api_key: str | None
    ) -> dict[str, Any]:
        """Try the request up to 1 + max_retries times, backing off between tries."""
        attempts: list[dict[str, Any]] = []  # history, useful for error-handling tests

        for attempt in range(self.max_retries + 1):
            result = self._send_once(route, payload, api_key)
            attempts.append(
                {k: result[k] for k in ("status", "latency_s", "error")}
            )

            retryable = result["status"] in RETRYABLE_STATUS or result["status"] is None
            if not retryable or attempt == self.max_retries:
                break

            # Exponential backoff: ~2s, 4s, 8s, 16s ... plus random "jitter" so
            # parallel threads don't all retry at exactly the same moment.
            # If the server tells us how long to wait (Retry-After), respect it.
            wait = result.get("retry_after") or (2 ** (attempt + 1))
            wait += random.uniform(0, 1)
            print(
                f"  [client] {route} -> {result['status'] or result['error']}; "
                f"retrying in {wait:.1f}s (attempt {attempt + 1}/{self.max_retries})"
            )
            time.sleep(wait)

        result.pop("retry_after", None)
        result["attempts"] = attempts
        return result

    def _send_once(
        self, route: str, payload: Any, api_key: str | None
    ) -> dict[str, Any]:
        """Send exactly one HTTP request and measure how long it takes."""
        # Reserve a slot in the budget *before* sending (under the lock).
        with self._lock:
            if self._request_count >= self.budget:
                raise BudgetExceeded(
                    f"Request budget of {self.budget} reached. "
                    "Use cached results or raise the budget deliberately."
                )
            self._request_count += 1

        headers = {
            "Authorization": f"Bearer {api_key if api_key is not None else self.api_key}",
            "Content-Type": "application/json",
        }
        status: int | None = None
        body: Any = None
        error: str | None = None
        retry_after: float | None = None

        # Wait for a free slot (max_parallel), then send.
        with self._semaphore:
            started = datetime.now(timezone.utc)
            t0 = time.perf_counter()  # high-resolution timer for latency
            try:
                resp = self._http.post(
                    self.base_url + route,
                    headers=headers,
                    # `content=` (not `json=`) lets tests send invalid JSON as a string.
                    content=payload if isinstance(payload, str) else json.dumps(payload),
                )
                status = resp.status_code
                try:
                    body = resp.json()
                except ValueError:
                    body = resp.text  # not JSON (e.g. an HTML error page)
                if status >= 400:
                    error = f"HTTP {status}"
                if "retry-after" in resp.headers:
                    try:
                        retry_after = float(resp.headers["retry-after"])
                    except ValueError:
                        pass
            except httpx.TimeoutException:
                error = "timeout"
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
            latency = time.perf_counter() - t0

        rec = {
            "route": route,
            "request": payload,
            "status": status,
            "latency_s": round(latency, 3),
            "error": error,
            "timestamp": started.isoformat(),
            "response": body,
            "retry_after": retry_after,
        }
        self._log(rec)
        return rec

    def _log(self, rec: dict[str, Any]) -> None:
        """Append one line per real HTTP call. Doubles as the budget counter."""
        line = {k: rec[k] for k in ("timestamp", "route", "status", "latency_s", "error")}
        # Store a short preview of the request, never the API key.
        line["request_preview"] = str(rec["request"])[:200]
        with self._lock:
            with REQUEST_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")

    @staticmethod
    def _count_logged_requests() -> int:
        if not REQUEST_LOG.exists():
            return 0
        with REQUEST_LOG.open(encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _cache_key(route: str, payload: Any, api_key: str | None) -> str:
    """Deterministic file name for a request.

    Same route + same payload -> same hash -> same cache file. `sort_keys`
    makes {"a":1,"b":2} and {"b":2,"a":1} hash the same. A custom api_key
    (only used in tests) is part of the key but hashed, so it never appears
    in plain text on disk.
    """
    body = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True)
    key_tag = "" if api_key is None else hashlib.sha256(api_key.encode()).hexdigest()
    raw = f"{route}\n{body}\n{key_tag}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _extract_contexts(body: Any) -> list[str]:
    """Pull the retrieved chunk texts out of a response, whatever its exact shape.

    We only know the endpoint from the outside, so this tries common layouts:
    a top-level list, or a list under keys like "contexts", "chunks",
    "results", "documents", "citations". Each item may be a plain string or a
    dict with the text under "text", "content", "chunk" or "page_content".
    The full raw body is always kept as well, so nothing is lost if this misses.
    """
    if not body or isinstance(body, str):
        return []

    items: Any = None
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        for k in ("contexts", "context", "chunks", "results", "documents",
                  "retrievalResults", "citations", "sources"):
            if isinstance(body.get(k), list):
                items = body[k]
                break

    if not items:
        return []

    texts = []
    for it in items:
        if isinstance(it, str):
            texts.append(it)
        elif isinstance(it, dict):
            for k in ("text", "content", "chunk", "page_content"):
                v = it.get(k)
                if isinstance(v, str):
                    texts.append(v)
                    break
                if isinstance(v, dict) and isinstance(v.get("text"), str):
                    texts.append(v["text"])  # e.g. Bedrock: {"content": {"text": ...}}
                    break
    return texts


def _extract_sources(body: Any) -> list[dict[str, Any]]:
    """Metadata for each retrieved chunk: which file, which page, what score.

    The endpoint returns e.g. {"text": ..., "source": "175 - WOLF.pdf",
    "page": 9, "score": 0.56}. This tells us *which document* was retrieved,
    which we need to check retrieval against the expected source document and
    to measure how much of the collection is reachable (coverage).
    """
    if not isinstance(body, dict) or not isinstance(body.get("contexts"), list):
        return []
    return [
        {k: c.get(k) for k in ("source", "page", "score")}
        for c in body["contexts"] if isinstance(c, dict)
    ]


# --------------------------------------------------------------------------- #
# Command-line smoke test
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send one question to the RAG endpoint.")
    parser.add_argument("question", nargs="?", default="What is WOLF?")
    parser.add_argument("--route", choices=["chat", "context", "both"], default="both")
    parser.add_argument("--no-cache", action="store_true", help="force a fresh request")
    parser.add_argument("--stats", action="store_true", help="only show requests used")
    args = parser.parse_args()

    # The Windows console defaults to cp1252, which can't print characters
    # like "≈" that appear in the papers. Switch stdout to UTF-8.
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    with RAGClient() as rag:
        if args.stats:
            print(f"Requests used so far: {rag.requests_used} / {rag.budget}")
            raise SystemExit

        if args.route in ("context", "both"):
            r = rag.context(args.question, use_cache=not args.no_cache)
            print(f"\n=== /v1/context  status={r['status']}  latency={r['latency_s']}s  "
                  f"cached={r['from_cache']}")
            print(f"{len(r['contexts'])} chunks parsed")
            for src in r["sources"]:
                print(f"  - {src['source']} (p. {src['page']}, score {src['score']})")
            print(json.dumps(r["raw"], ensure_ascii=False, indent=2)[:1500])

        if args.route in ("chat", "both"):
            r = rag.chat(args.question, use_cache=not args.no_cache)
            print(f"\n=== /v1/chat/completions  status={r['status']}  "
                  f"latency={r['latency_s']}s  cached={r['from_cache']}")
            print("Answer:", r["answer"])
            print(json.dumps(r["raw"], ensure_ascii=False, indent=2)[:1500])

        print(f"\nRequests used so far: {rag.requests_used} / {rag.budget}")
