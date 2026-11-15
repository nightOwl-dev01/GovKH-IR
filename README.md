# GovKH Search

Hybrid retrieval and cited answers over official Cambodian government documents, in Khmer and English.
A course project for Information and Web Retrieval at CADT (five members, five weeks).

The project compares lexical, dense and hybrid retrieval on the same corpus and benchmark:

| Research question | Compared |
|---|---|
| RQ1 Khmer segmentation | BM25 with no segmentation, word segmentation, character trigrams |
| RQ2 Semantic search | BM25 vs dense |
| RQ3 Hybrid | BM25, dense, Reciprocal Rank Fusion |
| RQ4 Reranking | Hybrid with and without a cross-encoder |
| RQ5 Status and recency | Hybrid with and without the boost |
| RQ6 Cross-lingual | English queries to Khmer documents and the reverse |

The full plan is in `Docs/GovKH Search Project Proposal.docx`. Task status is in [PROGRESS.md](PROGRESS.md).

## Status

| Part | State |
|---|---|
| Crawler and corpus manifest (`crawler/`) | Working. Collection is nearly complete. |
| Database schema, loader, OCR, parser, enrichment, chunking (`backend/govkh/`) | Working, run on the first 5,636 documents. |
| BM25 (three variants), TF-IDF, Boolean indexes | Working. |
| Dense index | Code written, not yet run on the corpus. |
| Hybrid ranking, RAG, API, web app, evaluation | Not started. |

## Repository layout

```
crawler/        Collection: whitelist crawler, provenance, deduplication, manifest and audit reports
backend/govkh/  Ingestion and retrieval package (PostgreSQL schema, OCR, parsing, chunking, indexes)
evaluation/     Benchmark queries, relevance judgments and experiment results (not started)
experiments/    An earlier small Khmer retrieval benchmark notebook
Docs/           Proposal and the crawl status report
PROGRESS.md     Task tracker: read this first
```

Collected files, extracted text, OCR output, indexes and the database are not committed (see `.gitignore`).

## Requirements

- Linux, Python 3.14, PostgreSQL with `pg_trgm` (developed on 18.6)
- Tesseract 5 with the Khmer model (`khm.traineddata`) and the English model
- Optional: an NVIDIA GPU for embedding, the `pgvector` extension, an OpenAI API key for metadata
  enrichment by language model and for RAG

## Setup

```bash
cd Project
python -m venv .venv
.venv/bin/pip install -e "crawler[dev]"
.venv/bin/pip install "psycopg[binary]" bm25s khmer-nltk ranx fastapi "uvicorn[standard]" pystemmer \
    openai pdfplumber hnswlib sentence-transformers pymupdf scipy
.venv/bin/playwright install chromium        # only for sources marked `render: true`
```

**Database.** Any PostgreSQL the `GOVKH_DSN` variable points to works. The development setup is a
user-owned cluster on port 5433:

```bash
initdb -D .local/pg -U govkh --auth=trust -E UTF8 --locale=C.UTF-8
echo "port = 5433" >> .local/pg/postgresql.conf
pg_ctl -D .local/pg -l .local/pg.log -w start
createdb -h 127.0.0.1 -p 5433 -U govkh govkh
```

Default DSN: `postgresql://govkh@127.0.0.1:5433/govkh`.

**Khmer OCR model.** Put `khm.traineddata` (from the `tesseract-ocr/tessdata_best` repository),
`eng.traineddata` and `osd.traineddata` in `backend/data/tessdata/`.

## Running

### 1. Collect (from `crawler/`)

```bash
../.venv/bin/python -m govkh_crawler audit       # reachability and robots.txt per source
./full_crawl.sh                                  # discover, download, build the manifest; resumable
../.venv/bin/python -m govkh_crawler status      # progress per source
```

Output: `crawler/data/manifest/manifest.jsonl` and `crawler/data/reports/corpus_report.md`.
Details, politeness rules and how to add a source: [crawler/README.md](crawler/README.md).

### 2. Build the indexes (from `backend/`)

```bash
P=../.venv/bin/python
$P -m govkh initdb          # create the schema
$P -m govkh load            # manifest -> sources, relations
$P -m govkh parse           # text per document; scanned or broken PDFs are queued for OCR
$P -m govkh ocr             # Tesseract khm+eng, cached per file in backend/data/ocr
$P -m govkh parse           # pick up the OCR results
$P -m govkh tables          # optional: tables of fee schedules and forms
$P -m govkh enrich          # document number, date, status, relations (add --llm to use the language model)
$P -m govkh chunk           # structure-aware parent/child chunks and the fixed-size baseline
$P -m govkh index           # BM25 x3, TF-IDF, Boolean
$P -m govkh embed           # dense vectors
```

`$P -m govkh all` runs the same steps in order. Every step is incremental: it only processes what is
new or changed, so it is safe to stop and rerun.

### Settings

`backend/govkh/config.py` holds the defaults. Environment variables override them:

| Variable | Default | Meaning |
|---|---|---|
| `GOVKH_DSN` | `postgresql://govkh@127.0.0.1:5433/govkh` | Database |
| `GOVKH_CRAWL_DATA` | `crawler/data` | Where the crawler wrote the corpus |
| `GOVKH_DATA` | `backend/data` | OCR cache and indexes |
| `GOVKH_EMBED_MODEL` | `BAAI/bge-m3` | Embedding model |
| `GOVKH_LLM_MODEL` | `gpt-4.1-mini` | Language model for enrichment and RAG |
| `OPENAI_API_KEY` | none | Needed only for `enrich --llm` and RAG |

## How it works

- **Official sources only.** The crawler requests only whitelisted government hosts, obeys robots.txt,
  sends about one request per second per host, and never bypasses bot protection.
- **Provenance.** Every document keeps its URL, SHA-256 checksum, collection time, institution and file type.
- **OCR.** A PDF page with almost no text is scanned. A PDF whose Khmer text layer is broken (legacy
  non-Unicode fonts) is detected by a well-formedness score and is also sent to OCR.
- **Normalisation.** NFC, zero-width characters removed, Khmer digits converted to ASCII in the search
  text. The original text is kept for display.
- **Chunking.** Legal instruments are split on ជំពូក (chapter) and មាត្រា / ប្រការ (article); other
  documents by paragraph. Small child chunks are searched and the larger parent is given to the language model.
  Each chunk is indexed with a header naming the institution, document type and number, date and article.
- **Indexes.** BM25 through `bm25s`, TF-IDF with cosine, a Boolean index with AND / OR / NOT, and
  PostgreSQL B-tree and trigram indexes for filters and document numbers.

## Known limitations

- MEF, NBC, MoI, TRC and MFAIC block declared crawlers and are not collected.
- The `pgvector` extension is not installed in the development database. The schema and code use it when
  present; without it, vectors live in a matrix file and approximate search uses `hnswlib`.
  The pgvector path has not been tested yet.
- OCR covers the first 40 pages of each scanned document.
- Document status (in force, amended, repealed) is known only where a source catalogue states it or
  another collected document says so. Most documents are `unknown`.
- Legacy `.doc` and `.xls` files are stored but not parsed.

## Disclaimer

GovKH Search provides information with links to official sources. It is not legal advice. Always check
the original document.

## Contributing and licence

See [CONTRIBUTING.md](CONTRIBUTING.md). Code is under the [MIT licence](LICENSE). The collected
government documents are not part of this repository and are not covered by that licence.
