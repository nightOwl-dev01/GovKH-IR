# GovKH Search: implementation tracker

Single source of truth for where the project stands. Read this first; do not re-read the proposal
(`Docs/GovKH Search Project Proposal.docx`) unless a section number below is not enough.
Update the status boxes and the "Last updated" line whenever work is done.

**Last updated:** 2026-11-15 16:40 (crawl nearly finished; Phase 2 built and run on the first 5,636 documents)

## 1. Project in five lines

- IR course project (CADT, 5 members, 5 weeks): hybrid retrieval + RAG over official Cambodian
  government documents, Khmer and English. Corpus target: **at least 10,001 unique documents**.
- Stack: React + TS + Vite, FastAPI, PostgreSQL + pgvector + pg_trgm, Python ingestion/retrieval, Docker Compose.
- Academic core: fair comparison of lexical vs dense vs hybrid. RQ1 Khmer segmentation for BM25,
  RQ2 dense vs BM25, RQ3 hybrid (RRF), RQ4 reranking, RQ5 status/recency boost, RQ6 cross-lingual.
- Priority order if time is short: verified corpus > BM25/dense/hybrid > evaluation > demo.
  Drop order: table/form extraction, E8, reranker, E7.
- Python env: `Project/.venv` (Python 3.14). Crawler deps plus psycopg, bm25s, khmer-nltk, ranx, fastapi, uvicorn,
  pystemmer, openai, pdfplumber, hnswlib, torch (CUDA), sentence-transformers.
- Repo docs: `README.md` (setup and run), `CONTRIBUTING.md` (team rules), `LICENSE` (MIT, code only).

## 2. Where we are

**Phase 1 crawl is nearly finished. Phase 2 (parse, OCR, enrich, chunk, lexical indexes) is built and was run
on the 2026-11-14 manifest (5,636 documents). Dense embedding, Phase 3, 4 and 5 are not built yet.**

| Figure (2026-11-15 16:35) | Value |
|---|---|
| Files downloaded by the crawler | 28,642 (1,294 pending: MRD 801, ODC 415, MAFF 74) |
| Retained in the manifest | 5,636, **stale**: `process` has not been rerun since the crawl resumed |
| Original data on disk | 24 GB in `crawler/data/raw` |
| Free disk space | **5.6 GB. Watch this.** |
| Loaded into PostgreSQL | 5,636 sources; 4,709 parsed, 925 waiting for the second `parse` (OCR done), 2 unsupported |
| OCR | 911 files done with Tesseract khm+eng, 0 failed (112 min, 12 workers) |
| Chunks | 27,202 structure children, 9,896 parents, 22,578 fixed-size |
| Lexical indexes | BM25 nosplit / word / trigram, TF-IDF, Boolean: built on the 4,709 parsed documents |

**Immediate next actions, in order:**
1. Wait for `full_crawl.sh` to finish (it runs `process` itself), then read the retained count in
   `crawler/data/reports/corpus_report.md`.
2. From `backend/`: `load`, `parse`, `ocr`, `parse`, `enrich`, `chunk`, `index` (section 5). All incremental.
3. Decide the embedding plan (see section 6: the GPU does about 4 chunks/s with BGE-M3), then `embed`.
4. Build `retrieval.py` (RRF, boost, rerank, grouping), then RAG, API, frontend, evaluation.

## 3. Phases and tasks

Status: `[x]` done, `[~]` partly done, `[ ]` not started. Section numbers refer to the proposal.

### Phase 1: Collection (Data Engineer, weeks 1-3) — `[~]`
- [x] Crawler package `crawler/govkh_crawler/` (whitelist, robots.txt, 1 req/s per host, retries, resumable SQLite state) — 8.1
- [x] Adapters: `ckan` (ODC), `wordpress`, `sitemap`, `crawl` (listing pages), `moeys` (site API); Playwright rendering (`render: true`); Google Drive embeds
- [x] Provenance per file: URL, SHA-256, collected_at, institution, file type — 6.5
- [x] Dedup: exact checksum, normalised text, MinHash near-duplicates; alternates kept — 6.4
- [x] Manifest (`data/manifest/manifest.jsonl|csv`), relations, change list, corpus report — 6.4, 6.5
- [x] Source audit (`data/reports/source_audit.md`) — 6.3
- [x] Excel status report: `Docs/GovKH_Crawl_Status_Report.xlsx` (rebuild: `report_xlsx.py`)
- [x] Tests: 14 pass (`../.venv/bin/python -m pytest` in `crawler/`)
- [~] Full crawl: 28,642 downloaded, 1,294 pending, still running (resumed 2026-11-15 11:17)
- [ ] Reach 10,001 retained: unknown until `process` reruns at the end of the crawl; if short, widen sources (section 6)
- [ ] MoEYS: firewall rejected ~300 file downloads. Retry slowly (`delay: 5` in sources.yaml, reset skipped rows) or drop
- [ ] Decide whether press releases count (`include_press` in `config/settings.yaml`; report shows both counts). Team or lecturer decision
- [ ] Freeze corpus snapshot (week 5)

### Phase 2: Parsing and indexing (Indexing Engineer, weeks 1-4) — `[~]`
Code: `backend/govkh/`. CLI: `python -m govkh <step>` from `backend/`.
- [x] PostgreSQL schema v1 (`schema.sql`): sources, documents, chunks, embeddings, doc_relations, assets, queries,
      relevance_judgments, search_runs, feedback; B-tree and pg_trgm indexes — 11
- [ ] Docker Compose (Docker daemon needs sudo to start; development uses a user-owned cluster, see section 6)
- [x] Loader (`ingest.load`): `manifest.jsonl` -> `sources`; `relations.jsonl` -> `doc_relations`; removes sources that left the manifest
- [x] OCR (`ingest.ocr`): Tesseract khm+eng at 200 dpi, first 40 pages per file, cached in `backend/data/ocr/<sha>.json`,
      `ocr_confidence` stored — 8.2
- [ ] Compare Tesseract with Surya or PaddleOCR on a sample — 8.2
- [x] Parsers (`ingest.parse`): PDF text layer per page (PyMuPDF, pages kept as form feeds), HTML/DOCX from crawler text.
      PDFs with a broken Khmer text layer (legacy fonts) are detected by `text.khmer_quality` < 0.95 and sent to OCR — 8.2
- [~] Tables (`ingest.tables`, pdfplumber -> `assets`): written, not run yet (droppable)
- [x] Khmer normalisation (`text.normalize`): NFC, ZWSP as a segmentation hint then removed, Khmer digits -> ASCII — 8.3
- [x] Tokenisation (`text.tokenize`): `nosplit`, `word` (khmer-nltk), `trigram`; English lowercase + Snowball stemming;
      document numbers kept whole and also indexed by part — 8.4
- [x] Chunking (`chunk.py`): chapter/article parent-child for legal instruments, paragraph-based for the rest,
      contextual header, fixed 512-token baseline with 64 overlap — 8.5
- [~] Metadata enrichment (`enrich.py`): rules done (GDT catalogue gives number, date and status for 258 documents;
      title and pre-preamble text rules; relations by cited number; status from relations). The language-model pass
      (`enrich --llm`) is written but has not been run. Only 9 phrase relations so far: rerun after OCR text is parsed — 8.6
- [~] Indexes: BM25 word / trigram / nosplit (bm25s), TF-IDF and Boolean (`lexical.py`) built; B-tree and pg_trgm in the
      schema. Vector index: `dense.py` written (exact matrix + hnswlib HNSW m=16, ef_construction=64; pgvector when present),
      **not run yet** — 9
- [ ] Embedding model choice on ~30 dev queries by Recall@20: BGE-M3, multilingual-e5-large, Qwen3-Embedding, EmbeddingGemma — 9.1
- [ ] Tests for `backend/govkh` (`backend/tests/` is empty)

### Phase 3: Retrieval (Retrieval Engineer, weeks 1-4) — `[ ]`
- [ ] Boolean, TF-IDF + cosine, BM25 x3 (no segmentation, word, trigram) — 10.1
- [ ] Dense search (pgvector), hybrid RRF (k=60, top 100 + 100) — 10.3
- [ ] Query processing: language detection, same normalisation, exact-reference detection, filters — 10.2
- [ ] Status and recency boost (tuned on dev, frozen before test) — 10.3
- [ ] Optional cross-encoder rerank of top 50 (droppable) — 10.3
- [ ] RAG: top 5-8 with parent sections, citations, "not found in official sources" refusal, repealed/amended warning — 10.4

### Phase 4: Evaluation (Evaluation Engineer, weeks 1-5) — `[ ]`
- [ ] 100-150 queries, Khmer + English, 9 query types, dev/test split — 12.1
- [ ] Pooling top 20 per method, graded 0-3, LLM pre-label + 30% human check, Cohen's kappa, TREC qrels — 12.2
- [ ] Metrics with ranx: nDCG@10 (main), Recall@50, MRR, P@5, MAP, latency p50/p95, index cost — 12.3
- [ ] Experiments E1-E8 and RAG vs no-retrieval on ~30 queries — 12.4, 12.5
- [ ] Report per query type and per language; error analysis — 12.6

### Phase 5: Application (Application Engineer, weeks 1-5) — `[ ]`
- [ ] FastAPI: search, document detail, answer, feedback, evaluation endpoints — 7
- [ ] React UI: bilingual search, method selector, filters, results with official badge and status warning, document view, RAG citations view, admin counts — 13.1
- [ ] Docker Compose for app + API + DB; README with run and demo steps — 17.1

### Phase 6: Integration and delivery (all, week 5) — `[ ]`
- [ ] Freeze corpus and configs, rerun benchmark, validate provenance
- [ ] Final report, slides, demo

## 4. Existing work outside the crawler

- `experiments/KhmerIR_Complete_Retrieval_Benchmark.ipynb` with `khmerir_results/` and `segmentation_gold.csv`:
  an earlier retrieval benchmark notebook by the user. Check it before building Phase 3; it may already
  cover BM25 variants and segmentation tests.
- `Docs/AI_Technology_Hybrid_Multimodal_IR_Project_Proposal.docx`: an older, different proposal. Ignore.

## 5. Quick reference

### Crawler (run from `Project/crawler`)

```bash
../.venv/bin/python -m govkh_crawler status                                   # per-source progress
setsid nohup ./full_crawl.sh >> data/logs/full_crawl.out 2>&1 < /dev/null &   # resume full crawl (detached)
kill -TERM $(pgrep -f "bash ./full_crawl.sh"); kill -INT $(pgrep -f "python -m govkh_crawler")   # stop cleanly
../.venv/bin/python -m govkh_crawler process                                  # rebuild manifest + report (offline)
../.venv/bin/python report_xlsx.py . ../Docs/GovKH_Crawl_Status_Report.xlsx   # rebuild Excel report
../.venv/bin/python -m pytest                                                 # tests
```

- Details: `crawler/README.md`. Registry: `crawler/config/sources.yaml`. Settings: `crawler/config/settings.yaml`.
- Do not run `pkill -f` / `kill $(pgrep -f ...)` from a shell command whose own text matches the pattern; it kills itself.
- Downstream input contract: `data/manifest/manifest.jsonl` (first 16 fields = `sources` table of section 11),
  originals at `data/<storage_uri>`, pre-extracted text at `data/<text_uri>`, `needs_ocr` flag, `relations.jsonl`.

### Backend (run from `Project/backend`)

```bash
pg_ctl -D ../.local/pg -l ../.local/pg.log -w start        # start the database (port 5433) after a reboot
psql -h 127.0.0.1 -p 5433 -U govkh govkh                   # SQL shell
P=../.venv/bin/python
$P -m govkh initdb; $P -m govkh load; $P -m govkh parse    # schema, manifest -> sources, text
$P -m govkh ocr --workers 12; $P -m govkh parse            # OCR (hours), then pick up the results
$P -m govkh enrich [--llm]; $P -m govkh chunk; $P -m govkh index; $P -m govkh embed
```

- Every step is incremental. Settings and env overrides: `backend/govkh/config.py`. Logs: `backend/data/logs/`.
- Indexes: `backend/data/index/<structure|fixed>/`. Python API: `lexical.get(strategy).bm25(query, mode, k)`,
  `.tfidf()`, `.boolean()`; `dense.get(strategy).search(query, k, ann=False)`.

## 6. Decisions and constraints already made

- `contact` in the crawler User-Agent stays empty (user decision).
- Google Drive files embedded on official pages are downloaded even though Drive's robots.txt disallows
  its download endpoint (user decision; `fetch_embedded_files` in settings).
- Bot protection is never bypassed. Not crawlable: MEF, NBC, MoI, TRC, MFAIC (challenge/WAF even in a
  headless browser). Also off: NSSF (no content), MLVT (HTTP 500), Senate (photo galleries), NIS and
  National Assembly (unreachable).
- Press releases are collected and flagged `is_press`; whether they count toward 10,001 is undecided.
- OCM press and MoE WordPress APIs are very slow (35-140 s per batch); MRD posts API returns 500, so MRD uses its sitemap.
- If the corpus falls short: add ODC `library_record` items issued by government bodies, retry NIS/National
  Assembly/MLVT, widen date ranges (proposal 6.3), or ask the lecturer about the counting rule.
- Database: Docker cannot be started without sudo, so development uses a user-owned PostgreSQL 18 cluster at
  `Project/.local/pg`, port 5433, user and database `govkh`, trust auth on localhost.
- pgvector is not installed (building it locally was blocked). `dense.py` uses pgvector when the extension exists and
  otherwise keeps vectors in a float16 matrix file with hnswlib for HNSW. The pgvector path is untested.
- Tesseract Khmer model: `backend/data/tessdata/khm.traineddata` (tessdata_best). About 2.5 s per page.
- GPU (GTX 1650, 4 GB): BGE-M3 runs at about 4.2 chunks/s in float32 for 400-500 token chunks; float16 is slower (1.0).
  A full corpus of ~150k chunks would take about 10 hours. Options: embed the `structure` strategy only, cap the
  sequence length, or pick a smaller model in the model-choice step.
- Language model: `OPENAI_API_KEY` is set in the environment and works. Default model `gpt-4.1-mini`. Not used yet.
- Evaluation is planned at document level (qrels keyed by `source_id`) so both chunking strategies share judgments.
- Licence: MIT for code; collected documents are not redistributed.
