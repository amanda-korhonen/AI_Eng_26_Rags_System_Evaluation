"""
coverage.py - Step 3: analyse the document collection and test retrieval coverage.

Three sub-commands, run in this order:

    python src/coverage.py inventory   # offline: inspect every PDF, find oddities
    python src/coverage.py run         # online: ~1 /v1/context request per document
    python src/coverage.py report      # offline: summarise results (Markdown)

1) INVENTORY (no endpoint requests)
   For every PDF in data/papers/ joined with the shared document list
   (data/meta/document_list.csv, exported from the Google Sheet), we record:
     - size, page count, extracted text length, characters per page
     - year (from the arXiv id, else the first page, else PDF metadata)
     - flags: duplicate, very large, likely scanned (no text layer),
       non-English, possibly not a scientific paper (blog/news/guide)
   Duplicates are detected on several levels, because a trainee upload can
   duplicate a paper in different ways:
     - exact:      identical file bytes (same SHA-256 hash)
     - same title: same normalised title in the sheet
     - same URL:   same source URL in the sheet
     - near:       different files but almost identical text (Jaccard
                   similarity of word 5-grams on the first pages >= 0.8),
                   e.g. an arXiv v1 and v2 of the same paper.
   Output: data/meta/inventory.csv, data/meta/duplicates.json

2) RUN (the coverage test)
   For each *unique* document (duplicates share one query) we send the
   paper's title to /v1/context and check whether any of the returned chunks
   comes from that document. Short or generic titles ("WOLF", "Cradle")
   get the first words of the abstract appended, so the query "should
   clearly hit" the paper. Documents that miss are retried ONCE with a
   distinctive phrase from the abstract (second chance), so we can tell
   "hard to find by title" apart from "not in the index at all".
   Budget: about 1 request per document, plus one retry per miss.
   Output: data/raw/coverage_results.jsonl (+ raw responses in the cache)

3) REPORT
   "X of N documents are retrievable; these are missing or unreachable",
   plus the list of oddities from the inventory, and what the index itself
   reveals (e.g. duplicate chunks, files in the index not in the list).
   Output: data/meta/coverage_report.md
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAPERS_DIR = PROJECT_ROOT / "data" / "papers"
META_DIR = PROJECT_ROOT / "data" / "meta"
DOC_LIST = META_DIR / "document_list.csv"
INVENTORY_CSV = META_DIR / "inventory.csv"
DUPLICATES_JSON = META_DIR / "duplicates.json"
RESULTS_JSONL = PROJECT_ROOT / "data" / "raw" / "coverage_results.jsonl"
REPORT_MD = META_DIR / "coverage_report.md"

# Thresholds for the flags (documented so the report can quote them).
LARGE_FILE_MB = 20           # "very large" file
SCANNED_CHARS_PER_PAGE = 200  # below this, the PDF probably has no text layer
NEAR_DUP_JACCARD = 0.8       # text similarity above which two files are "the same"
TEXT_PAGES = 3               # "first pages" used for abstract / title / near-dup check
EMPTY_PAGE_SHARE = 0.3       # flag if more than this share of pages has no text
TITLE_MATCH_MIN = 0.6        # flag if fewer title words than this appear in the text
TEXT_DIR = META_DIR / "text"  # extracted full text, one file per document

# A small list of very common English words. If a text contains few of them,
# it is probably not English (or the text extraction produced garbage).
EN_STOPWORDS = set("""the of and to in a is that for are with as on this be by we
an it from or which our can at not these their have has more than such
its was were also use using used based""".split())

# Domains of scholarly sources. A URL elsewhere (blogs, company pages, news)
# hints that the document may not be a peer-reviewed / preprint paper.
SCHOLARLY_HINTS = ("arxiv.org", "doi.org", "acm.org", "neurips.cc", "aclanthology",
                   "openreview", "sciencedirect", "springer", "ieee", "frontiersin",
                   "nature.com/articles/s4", "iza.org", "pnas", "science.org/doi",
                   "researchgate", "ssrn", "proceedings", "mdpi", "wiley", "plos")


# =========================================================================== #
# 1) INVENTORY
# =========================================================================== #

def normalise(text: str) -> str:
    """Lower-case, strip accents and punctuation: 'Agent-as-a-Judge:' -> 'agent as a judge'."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return text.strip()


def load_document_list() -> dict[int, dict[str, str]]:
    """Read the sheet export. Row 1 is a heading, row 2 the column names.

    Keyed by *row position* (1, 2, 3, ...), NOT by the sheet's "Num" column:
    the sheet uses Num 92 twice, so from row 93 on its Num is one lower than
    the row position. The PDF files are numbered by row position
    (001 ... 183), so position is the reliable join key. The sheet's own Num
    is kept as "sheet_num" so the report can show the inconsistency.
    """
    rows: dict[int, dict[str, str]] = {}
    with DOC_LIST.open(encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)            # "AI Engineering Course Literature, ..."
        header = next(reader)   # Num, Title, URL, Category
        for r in reader:
            if not r or not r[0].strip().isdigit():
                continue
            d = dict(zip(header, r))
            rows[len(rows) + 1] = d
    return rows


def title_matches(file_name: str, title: str) -> bool:
    """Sanity check for the join: does the file name look like the sheet title?

    File names drop characters like ':' and '?' and may be truncated, so we
    compare normalised text and allow the file name to be a prefix.
    """
    f = normalise(re.sub(r"^\d+\s*-\s*|\.pdf$", "", file_name))
    t = normalise(title)
    return bool(f) and (t.startswith(f[:60]) or f.startswith(t[:60]))


def inspect_pdf(path: Path) -> dict[str, Any]:
    """Open one PDF and collect facts about it. Runs in a worker process.

    We extract the text of EVERY page (what a RAG ingestion pipeline would
    see) and save it to data/meta/text/NNN.txt, which is reused later to
    write test questions. We deliberately catch every exception: a PDF that
    cannot be opened is itself a finding (it probably failed ingestion too).
    """
    import logging
    from pypdf import PdfReader  # imported here so worker processes load it
    # pypdf logs a warning for every odd font; these are harmless for us.
    logging.getLogger("pypdf").setLevel(logging.ERROR)

    data = path.read_bytes()
    info: dict[str, Any] = {
        "file": path.name,
        "size_mb": round(len(data) / 1e6, 2),
        "sha256": hashlib.sha256(data).hexdigest(),
        # A complete PDF ends with "%%EOF" (possibly followed by whitespace).
        # Its absence suggests a truncated download/upload.
        "truncated": b"%%EOF" not in data[-2048:],
        "pages": None, "chars_total": 0, "chars_per_page": 0,
        "empty_page_share": None, "first_text": "", "full_text": "",
        "pdf_year": None, "open_error": None,
    }
    try:
        reader = PdfReader(str(path))
        n = len(reader.pages)
        info["pages"] = n
        texts = []
        for page in reader.pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                texts.append("")
        # Pages with (almost) no text are images: scanned pages, slides,
        # full-page figures. A RAG system indexes nothing from them.
        info["empty_page_share"] = round(
            sum(len(t.strip()) < SCANNED_CHARS_PER_PAGE for t in texts) / max(n, 1), 2)
        info["chars_total"] = sum(len(t) for t in texts)
        info["chars_per_page"] = round(info["chars_total"] / max(n, 1))
        info["first_text"] = "\n".join(texts[:TEXT_PAGES])[:12000]
        info["full_text"] = "\n\f".join(texts)  # \f (form feed) marks a page break

        # PDF metadata creation date looks like "D:20240115...".
        meta = reader.metadata or {}
        m = re.search(r"(19|20)\d\d", str(meta.get("/CreationDate", "")))
        info["pdf_year"] = int(m.group(0)) if m else None
    except Exception as exc:
        info["open_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return info


def title_in_text(title: str, text: str) -> float:
    """Share of the title's content words that occur in the first pages.

    ~1.0 means the PDF is the paper the sheet says it is. A low value means
    the file probably contains a *different* document (wrong file uploaded).
    """
    words = [w for w in normalise(title).split() if len(w) > 3 and w not in EN_STOPWORDS]
    if not words:
        return 1.0
    body = set(normalise(text).split())
    return round(sum(w in body for w in words) / len(words), 2)


def guess_year(url: str, first_text: str, pdf_year: int | None) -> tuple[int | None, str]:
    """Best-effort publication year, and where it came from."""
    # arXiv ids encode year+month: arxiv.org/abs/2402.05120 -> 2024
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{2})(\d{2})\.\d{4,5}", url)
    if m:
        return 2000 + int(m.group(1)), "arxiv id"
    # Otherwise, the most frequent plausible year on the first pages.
    years = [int(y) for y in re.findall(r"\b(19[5-9]\d|20[0-2]\d)\b", first_text[:5000])]
    if years:
        return Counter(years).most_common(1)[0][0], "first pages"
    if pdf_year:
        return pdf_year, "pdf metadata"
    return None, "unknown"


def english_score(text: str) -> float:
    """Share of words that are common English stop-words (English text: ~0.3-0.5)."""
    words = re.findall(r"[a-zA-Z]+", text.lower())
    if len(words) < 50:
        return 0.0
    return sum(w in EN_STOPWORDS for w in words) / len(words)


def shingles(text: str, k: int = 5) -> set[str]:
    """Set of word k-grams; two texts sharing most k-grams are near-duplicates."""
    words = normalise(text).split()
    return {" ".join(words[i:i + k]) for i in range(max(len(words) - k + 1, 0))}


def extract_abstract(text: str, n_words: int = 30) -> str:
    """First `n_words` words after 'Abstract', or of the text if there is none."""
    m = re.search(r"\babstract\b[\s.:—-]*", text, flags=re.IGNORECASE)
    body = text[m.end():] if m else text
    words = re.sub(r"\s+", " ", body).split()
    return " ".join(words[:n_words])


def cmd_inventory(args: argparse.Namespace) -> None:
    docs = load_document_list()
    pdfs = sorted(PAPERS_DIR.glob("*.pdf"))
    print(f"Sheet lists {len(docs)} documents; {len(pdfs)} PDFs in {PAPERS_DIR}")
    nums = Counter(d["Num"] for d in docs.values())
    repeated = [n for n, c in nums.items() if c > 1]
    if repeated:
        print(f"NOTE: the sheet repeats Num {repeated}; joining files by row position.")

    # PDF parsing is CPU-heavy (1 GB of PDFs), so use several processes.
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        infos = list(pool.map(inspect_pdf, pdfs, chunksize=2))

    # ---- join each PDF with its sheet row via the "NNN - " number prefix ----
    rows = []
    for info in infos:
        m = re.match(r"(\d+)\s*-\s*", info["file"])
        num = int(m.group(1)) if m else None
        sheet = docs.get(num, {})
        title = sheet.get("Title", "")
        url = sheet.get("URL", "")
        if sheet and not title_matches(info["file"], title):
            print(f"WARNING: file/sheet title mismatch at {num}: "
                  f"{info['file']!r} vs {title!r}")
        year, year_src = guess_year(url, info["first_text"], info["pdf_year"])
        # Save the extracted text (used later to write test questions).
        if num is not None:
            TEXT_DIR.mkdir(parents=True, exist_ok=True)
            (TEXT_DIR / f"{num:03d}.txt").write_text(info["full_text"], encoding="utf-8", errors="replace")
        rows.append({
            "num": num, "sheet_num": sheet.get("Num", ""),
            "file": info["file"], "title": title, "url": url,
            "category": sheet.get("Category", ""), "year": year, "year_source": year_src,
            "size_mb": info["size_mb"], "pages": info["pages"],
            "chars_per_page": info["chars_per_page"],
            "empty_page_share": info["empty_page_share"],
            "truncated": info["truncated"],
            # Language check on the whole text: the first pages are often
            # dominated by author lists, which look "non-English".
            "english_score": round(english_score(info["full_text"][:200000]), 3),
            "title_in_text": title_in_text(title, info["first_text"]),
            "sha256": info["sha256"], "open_error": info["open_error"],
            "abstract_snippet": extract_abstract(info["first_text"]),
            "_text": info["first_text"],
        })
    rows.sort(key=lambda r: r["num"] or 0)

    missing_files = sorted(set(docs) - {r["num"] for r in rows})
    if missing_files:
        print(f"WARNING: in the sheet but no PDF: {missing_files}")

    # ---- duplicate detection ----
    def groups_by(keyfunc) -> list[list[int]]:
        g: dict[str, list[int]] = defaultdict(list)
        for r in rows:
            k = keyfunc(r)
            if k:
                g[k].append(r["num"])
        return [v for v in g.values() if len(v) > 1]

    dup = {
        "exact_file": groups_by(lambda r: r["sha256"]),
        "same_title": groups_by(lambda r: normalise(r["title"])),
        "same_url": groups_by(lambda r: r["url"].strip().rstrip("/").lower()
                              .replace("/pdf/", "/abs/").removesuffix(".pdf")),
        "near_text": [],
    }
    # Near-duplicates: compare text fingerprints pairwise (183^2/2 ~ 17k pairs: fast).
    sh = {r["num"]: shingles(r["_text"][:6000]) for r in rows}
    nums = [r["num"] for r in rows if len(sh[r["num"]]) > 50]
    for i, a in enumerate(nums):
        for b in nums[i + 1:]:
            inter = len(sh[a] & sh[b])
            if inter and inter / len(sh[a] | sh[b]) >= NEAR_DUP_JACCARD:
                dup["near_text"].append([a, b])

    # Merge all duplicate relations into clusters (union-find), and pick the
    # lowest number in each cluster as the "canonical" copy.
    parent = {r["num"]: r["num"] for r in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for kind in dup.values():
        for group in kind:
            for other in group[1:]:
                ra, rb = find(group[0]), find(other)
                parent[max(ra, rb)] = min(ra, rb)
    clusters: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        clusters[find(r["num"])].append(r["num"])
    dup["clusters"] = [sorted(v) for v in clusters.values() if len(v) > 1]

    # ---- flags ----
    for r in rows:
        canon = find(r["num"])
        r["canonical"] = canon
        flags = []
        if canon != r["num"]:
            flags.append(f"duplicate_of_{canon:03d}")
        if r["open_error"]:
            flags.append("unreadable_pdf")
        if r["size_mb"] >= LARGE_FILE_MB:
            flags.append("very_large")
        if r["truncated"]:
            flags.append("truncated_file")
        if r["pages"] and r["chars_per_page"] < SCANNED_CHARS_PER_PAGE:
            flags.append("likely_scanned_no_text")
        elif r["empty_page_share"] and r["empty_page_share"] > EMPTY_PAGE_SHARE:
            flags.append("many_pages_without_text")
        if r["title"] and r["title_in_text"] < TITLE_MATCH_MIN:
            flags.append("title_not_in_text")
        if r["chars_per_page"] >= SCANNED_CHARS_PER_PAGE and r["english_score"] < 0.12:
            flags.append("possibly_non_english")
        if r["url"] and not any(h in r["url"].lower() for h in SCHOLARLY_HINTS):
            flags.append("possibly_non_paper")
        if r["pages"] and r["pages"] <= 3:
            flags.append("very_short")
        if r["pages"] and r["pages"] >= 100:
            flags.append("very_long")
        r["flags"] = ";".join(flags)

    # ---- write outputs ----
    META_DIR.mkdir(parents=True, exist_ok=True)
    cols = ["num", "sheet_num", "file", "title", "url", "category", "year", "year_source",
            "size_mb", "pages", "chars_per_page", "empty_page_share", "truncated", "english_score",
            "title_in_text", "canonical",
            "flags", "open_error", "sha256", "abstract_snippet"]
    with INVENTORY_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    DUPLICATES_JSON.write_text(json.dumps(dup, indent=2), encoding="utf-8")

    # ---- console summary ----
    print(f"\nWrote {INVENTORY_CSV.relative_to(PROJECT_ROOT)} and "
          f"{DUPLICATES_JSON.relative_to(PROJECT_ROOT)}")
    for kind, groups in dup.items():
        print(f"  {kind:11s}: {groups}")
    unique = len({r['canonical'] for r in rows})
    print(f"\n{len(rows)} files -> {unique} unique documents")
    flag_counts = Counter(f.split("_of_")[0] for r in rows for f in r["flags"].split(";") if f)
    for flag, c in flag_counts.most_common():
        print(f"  {flag:24s} {c}")
    print("\nCategories:", dict(Counter(r["category"] for r in rows)))
    print("Years:", dict(sorted(Counter(r["year"] for r in rows if r["year"]).items())))


# =========================================================================== #
# 2) RUN - the coverage test against /v1/context
# =========================================================================== #

def read_inventory() -> list[dict[str, Any]]:
    if not INVENTORY_CSV.exists():
        sys.exit("Run `python src/coverage.py inventory` first.")
    with INVENTORY_CSV.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["num"] = int(r["num"])
        r["canonical"] = int(r["canonical"])
    return rows


def source_num(source: str | None) -> int | None:
    """'175 - WOLF.pdf' -> 175. The endpoint reports the uploaded file name."""
    m = re.match(r"\s*(\d+)\s*-", source or "")
    return int(m.group(1)) if m else None


def title_query(r: dict[str, Any]) -> str:
    """Query 1: the title. Short/generic titles get the abstract start appended."""
    title = r["title"] or r["file"]
    if len(title.split()) < 4:
        title = f"{title}: {' '.join(r['abstract_snippet'].split()[:15])}"
    return title


def check_hit(sources: list[dict[str, Any]], members: set[int]) -> dict[str, Any]:
    """Did any returned chunk come from the target document (or its duplicate)?"""
    nums = [source_num(s.get("source")) for s in sources]
    ranks = [i + 1 for i, n in enumerate(nums) if n in members]
    return {
        "hit": bool(ranks),
        "best_rank": ranks[0] if ranks else None,       # 1 = top result
        "n_chunks_from_doc": len(ranks),
        "returned_nums": nums,
    }


def cmd_run(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path(__file__).parent))
    from client import RAGClient

    rows = read_inventory()
    clusters: dict[int, set[int]] = defaultdict(set)
    for r in rows:
        clusters[r["canonical"]].add(r["num"])
    # One query per unique document: the canonical (lowest-numbered) copy.
    targets = [r for r in rows if r["num"] == r["canonical"]]
    if args.limit:
        targets = targets[: args.limit]

    rag = RAGClient(max_parallel=2)
    print(f"Coverage test on {len(targets)} unique documents "
          f"(requests used so far: {rag.requests_used}/{rag.budget})")

    def test_one(r: dict[str, Any]) -> dict[str, Any]:
        members = clusters[r["num"]]
        attempts = []
        queries = [("title", title_query(r))]
        # Second chance with a distinctive abstract phrase, only used on a miss.
        if r["abstract_snippet"] and not args.no_retry:
            queries.append(("abstract", r["abstract_snippet"]))

        for kind, q in queries:
            res = rag.context(q)
            h = check_hit(res["sources"], members)
            attempts.append({
                "query_type": kind, "query": q, "status": res["status"],
                "error": res["error"], "latency_s": res["latency_s"],
                "n_chunks": len(res["sources"]), **h,
                "sources": res["sources"],
            })
            if h["hit"] or res["status"] != 200:
                break

        last = attempts[-1]
        return {
            "num": r["num"], "title": r["title"], "members": sorted(members),
            "retrievable": any(a["hit"] for a in attempts),
            "found_by": next((a["query_type"] for a in attempts if a["hit"]), None),
            "best_rank": next((a["best_rank"] for a in attempts if a["hit"]), None),
            "error": last["error"], "attempts": attempts,
        }

    # Two threads -> at most 2 requests in flight (the client enforces it too).
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(test_one, targets))

    RESULTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_JSONL.open("w", encoding="utf-8") as f:
        for res in results:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")

    ok = sum(r["retrievable"] for r in results)
    print(f"\n{ok} of {len(results)} unique documents are retrievable.")
    for r in results:
        if not r["retrievable"]:
            print(f"  MISSING {r['num']:03d}  {r['title'][:70]}  (error: {r['error']})")
    print(f"Requests used so far: {rag.requests_used}/{rag.budget}")
    rag.close()


# =========================================================================== #
# 3) REPORT
# =========================================================================== #

def cmd_report(args: argparse.Namespace) -> None:
    rows = read_inventory()
    by_num = {r["num"]: r for r in rows}
    dup = json.loads(DUPLICATES_JSON.read_text(encoding="utf-8"))
    results = [json.loads(l) for l in RESULTS_JSONL.read_text(encoding="utf-8").splitlines()
               if l.strip()] if RESULTS_JSONL.exists() else []

    out: list[str] = ["# Document collection and coverage report", ""]

    # ---- collection overview ----
    unique = len({r["canonical"] for r in rows})
    out += ["## Collection", "",
            f"- Files: **{len(rows)}**, unique documents after de-duplication: **{unique}**",
            f"- Total size: {sum(float(r['size_mb']) for r in rows):.0f} MB",
            "- Categories: " + ", ".join(f"{k} ({v})" for k, v in
                                         Counter(r["category"] for r in rows).most_common()),
            "- Years: " + ", ".join(f"{k} ({v})" for k, v in sorted(
                Counter(r["year"] for r in rows if r["year"]).items())),
            ""]

    # ---- problems with the document list itself ----
    repeated = [n for n, c in Counter(r["sheet_num"] for r in rows).items() if c > 1]
    mismatched = [r for r in rows if "title_not_in_text" in r["flags"]]
    out += ["## Document list and file problems", ""]
    if repeated:
        first_off = next(r["num"] for r in rows if r["sheet_num"] != str(r["num"]))
        out.append(f"- The sheet uses Num {', '.join(repeated)} twice, so from row "
                   f"{first_off} on the sheet's Num is one lower than the file number. "
                   f"The sheet appears to list {len(rows) - len(repeated)} documents; "
                   f"there are {len(rows)}.")
    for r in mismatched:
        out.append(f"- **Wrong file:** {r['num']:03d} should be \"{r['title']}\", but only "
                   f"{float(r['title_in_text']):.0%} of the title words occur in its text, "
                   "so it contains a different document (inspect manually).")
    out.append("")

    out += ["## Duplicates", ""]
    for cl in dup["clusters"]:
        kinds = [k for k, groups in dup.items() if k != "clusters"
                 and any(set(g) & set(cl) for g in groups)]
        out.append(f"- {', '.join(f'{n:03d}' for n in cl)}: \"{by_num[cl[0]]['title']}\" "
                   f"(detected by: {', '.join(kinds)})")
    out.append("")

    out += ["## Other oddities (flagged automatically; review manually)", "",
            "| # | Title | Size MB | Pages | Chars/page | Empty pages | Title in text | Flags |",
            "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        flags = [f for f in r["flags"].split(";") if f and not f.startswith("duplicate")]
        if flags:
            out.append(f"| {r['num']:03d} | {r['title'][:60]} | {r['size_mb']} | "
                       f"{r['pages']} | {r['chars_per_page']} | {r['empty_page_share']} | "
                       f"{r['title_in_text']} | {', '.join(flags)} |")
    out.append("")

    # ---- coverage ----
    if results:
        ok = [r for r in results if r["retrievable"]]
        by_title = sum(r["found_by"] == "title" for r in ok)
        ranks = Counter(r["best_rank"] for r in ok)
        by_abstract = ", ".join(f"{r['num']:03d}" for r in ok if r["found_by"] != "title")
        wrong_nums = {m["num"] for m in mismatched}
        n_wrong = sum(r["num"] in wrong_nums for r in ok)
        out += ["## Coverage (retrieval via /v1/context)", "",
                f"**{len(ok)} of {len(results)} unique documents are retrievable** "
                f"({len(ok) / len(results):.0%}).",
                f"- Found by title query: {by_title}; only by abstract phrase: "
                f"{len(ok) - by_title} ({by_abstract or '-'})",
                # A wrong file is "retrievable" (the file is indexed) but the paper
                # the sheet promises is not in the knowledge base.
                f"- Of these, {n_wrong} contain the wrong document (see above), so "
                f"**the intended content is available for {len(ok) - n_wrong} of "
                f"{len(results)} documents**.",
                "- Rank of first matching chunk: " +
                ", ".join(f"#{k}: {v}" for k, v in sorted(ranks.items())),
                "", "### Missing or unreachable", "",
                "Neither the exact title nor a verbatim abstract phrase retrieved any chunk "
                "of these documents, and no chunk of them appeared in any other response "
                "either, so they are most likely not in the index (failed ingestion) rather "
                "than just hard to retrieve.", "",
                "| # | Title | Size MB | Pages | Possible cause | Top returned docs (title query) |",
                "|---|---|---|---|---|---|"]
        for r in results:
            if not r["retrievable"]:
                inv = by_num[r["num"]]
                got = r["attempts"][0]["returned_nums"]
                # Amazon Bedrock Knowledge Bases (Titan + Nova suggest Bedrock)
                # skip source files above 50 MB. Otherwise we cannot tell from
                # outside; the ingestion job log would show the reason.
                cause = ("file > 50 MB (likely above the ingestion size limit)"
                         if float(inv["size_mb"]) > 50 else "unknown (check ingestion log)")
                out.append(f"| {r['num']:03d} | {r['title'][:60]} | {inv['size_mb']} | "
                           f"{inv['pages']} | {cause} | {', '.join(str(n) for n in got)} |")
        out.append("")

        # What the index itself tells us, from every chunk we saw.
        # We match index sources to local files by their number prefix, not
        # the full name: the index keeps characters such as ':' that Windows
        # file names cannot contain (they became '_' locally).
        seen_sources: Counter = Counter()
        for r in results:
            for a in r["attempts"]:
                for s in a["sources"]:
                    seen_sources[s.get("source")] += 1
        seen_nums = {source_num(s) for s in seen_sources}
        unknown = [s for s in seen_sources if source_num(s) not in by_num]
        never = [r["num"] for r in rows if r["num"] not in seen_nums]
        renamed = sorted(s for s in seen_sources
                         if source_num(s) in by_num and s != by_num[source_num(s)]["file"])
        out += ["### What the index reveals", "",
                f"- Distinct source files seen in any response: {len(seen_sources)} "
                f"(of {len(rows)} files)",
                f"- Sources in the index that are not in data/papers: {unknown or 'none'}",
                f"- Files never returned by any coverage query: {len(never)}"
                + (f" ({', '.join(f'{n:03d}' for n in never)})" if never else ""),
                f"- Index file names that differ from the local copy (only ':' vs '_'): "
                f"{len(renamed)}",
                ""]
        dup_hits = [r for r in results if len(r["members"]) > 1 and r["retrievable"]]
        if dup_hits:
            out += ["- Duplicates in the index: for duplicated documents, the returned chunks "
                    "came from these copies (both copies ingested = wasted top-k slots):", ""]
            for r in dup_hits:
                a = next(a for a in r["attempts"] if a["hit"])
                c = Counter(n for n in a["returned_nums"] if n in r["members"])
                out.append(f"  - {r['title'][:50]}: " +
                           ", ".join(f"{n:03d}×{k}" for n, k in sorted(c.items())))
            out.append("")

    REPORT_MD.write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))
    print(f"\nWrote {REPORT_MD.relative_to(PROJECT_ROOT)}")


# =========================================================================== #

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("inventory", help="inspect PDFs offline, detect duplicates/oddities")
    s.add_argument("--workers", type=int, default=4, help="parallel PDF parsing processes")
    s = sub.add_parser("run", help="coverage test: query /v1/context per document")
    s.add_argument("--limit", type=int, default=0, help="only test the first N (for a trial)")
    s.add_argument("--no-retry", action="store_true", help="skip the abstract-phrase retry")
    sub.add_parser("report", help="write data/meta/coverage_report.md")
    args = p.parse_args()
    {"inventory": cmd_inventory, "run": cmd_run, "report": cmd_report}[args.cmd](args)
