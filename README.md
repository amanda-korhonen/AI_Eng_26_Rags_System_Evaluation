# LiteratureExplorer RAG Evaluation

This project evaluates **LiteratureExplorer**, a RAG system built by XYZ Inc. that answers questions about a collection of AI-engineering papers. It is a black-box evaluation: the system is tested only through its public API.

The work is done in steps. This README covers the steps implemented so far:

| Step | Script | What it does | Endpoint requests |
|---|---|---|---|
| – | `src/client.py` | Shared client for the RAG endpoint (caching, retries, rate limit, request budget) | – |
| 3 | `src/coverage.py` | Analyses the document collection and tests whether every document can be retrieved | ~1 per document |

Ragas version: 0.4.4

---

## 1. Setup

**Requirements:** Python 3.10 or newer.

```bash
# Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

# Install the dependencies
pip install -r requirements.txt
```

### API keys (`.env`)

The API URLs and keys are **not** included in this repository. Create a file called `.env` in the project root with the following content and fill in the values you were given:

```ini
# The RAG system under test
RAG_BASE_URL=https://...
RAG_API_KEY=...

# Lehmus AI models (used as judge/embeddings in later steps; university network or VPN only)
LEHMUS_BASE_URL=https://...
LEHMUS_API_KEY=...
JUDGE_MODEL=...
FAST_MODEL=...
EMBED_MODEL=...
```

Only `RAG_BASE_URL` and `RAG_API_KEY` are needed for the steps described here. `.env` is listed in `.gitignore` and must never be committed.

### Source documents (only needed for the inventory step)

The papers are not in the repository because they total over 1 GB. To run the inventory step yourself, download `papers.zip` from the link in the assignment and extract the PDFs into `data/papers/`, so that the files are named like `data/papers/001 - Attention Is All You Need.pdf`.

The document list (an export of the shared Google Sheet) is included as `data/meta/document_list.csv`.

---

## 2. Running the scripts

Run all commands from the project root with the virtual environment activated.

### Check that the endpoint works

```bash
python src/client.py "What is WOLF?"         # both routes: retrieval and answer
python src/client.py --route context "..."   # retrieval only
python src/client.py --route chat "..."      # answer only
python src/client.py --stats                 # how many endpoint requests have been used
```

### Step 3: Document collection and coverage

Run the three sub-commands in this order:

```bash
python src/coverage.py inventory    # offline, about 1-2 minutes
python src/coverage.py run          # calls the endpoint, about 30 seconds
python src/coverage.py report       # offline, writes the summary
```

**`inventory`** reads every PDF in `data/papers/` and the document list. It makes no endpoint requests. For each document it records the size, page count, how much text could be extracted, the year and the category, and it flags:

- **duplicates** (identical files, same title, same URL, or near-identical text)
- **wrong files** (the title from the document list does not appear in the PDF)
- **likely scanned PDFs** or files with many pages without text
- **possibly non-English** documents
- **possibly not a research paper** (blog posts, guides, news articles)
- **very large, very long, very short, or truncated** files

Option: `--workers N` sets the number of parallel processes for reading PDFs (default 4).

**`run`** is the coverage test. For each unique document (duplicates are tested once), it sends the paper's title to `/v1/context` and checks whether any returned chunk comes from that document. If not, it retries once with a phrase from the paper's abstract. This uses about 180–190 endpoint requests.

Options:
- `--limit N` tests only the first N documents (useful for a quick trial)
- `--no-retry` skips the second attempt with the abstract phrase

**`report`** combines the results into `data/meta/coverage_report.md`: how many documents are retrievable, which are missing, and what else was found (duplicates in the index, wrong files, problems with the document list).

---

## 3. Re-running without calling the endpoint

Every endpoint response is cached in `data/raw/cache/`. Running a script again reads the saved responses instead of sending new requests, so **the results can be reproduced without access to the endpoint** (and without an API key, as long as the cache is complete).

To force fresh requests (for example, to measure latency again), delete the relevant cache files or use `--no-cache` with `client.py`.

---

## 4. Output files

| File | Contents |
|---|---|
| `data/meta/document_list.csv` | The document list as exported from the Google Sheet |
| `data/meta/inventory.csv` | One row per PDF, with all measurements and flags |
| `data/meta/duplicates.json` | Duplicate groups, by detection method |
| `data/meta/coverage_report.md` | Human-readable summary of step 3 |
| `data/raw/coverage_results.jsonl` | Coverage test result per document, including the queries and returned chunks |
| `data/raw/cache/*.json` | Raw endpoint responses (request, response, status, latency, timestamp) |
| `data/raw/request_log.jsonl` | One line per real request sent to the endpoint |
| `data/meta/text/*.txt` | Extracted full text of each PDF (not committed; recreated by `inventory`) |

---

## 5. How the client respects the endpoint's usage rules

All scripts call the endpoint through `src/client.py`, which enforces the assignment's rules:

- **At most 2 requests in parallel.**
- **Retries with increasing waits** (about 2, 4, 8 and 16 seconds) on HTTP 429, 502, 503, 504 or network errors. It never retries immediately in a loop.
- **Request budget:** every real request is logged, and the client refuses to send more than 950 in total across all runs. Check usage with `python src/client.py --stats`.
- **Caching:** an identical request is never sent twice.
- **No hard-coded secrets:** the URLs and keys are read from `.env`. API keys are never written to the cache or the log.

---

## 6. Project structure

```
.
├── src/
│   ├── client.py          # RAG endpoint client (used by all scripts)
│   ├── coverage.py        # Step 3: document inventory and coverage test
│   └── ...                # further steps (test set, Ragas evaluation, system tests)
├── data/
│   ├── papers/            # source PDFs (not committed, download separately)
│   ├── meta/              # document list, inventory, reports
│   └── raw/               # raw endpoint responses and request log
├── requirements.txt
└── README.md
```
