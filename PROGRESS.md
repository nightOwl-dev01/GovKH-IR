# GovKH Search: implementation tracker

Single source of truth for where the project stands. Read this first; do not re-read the proposal
(`Docs/GovKH Search Project Proposal.docx`) unless a section number below is not enough.
Update the status boxes and the "Last updated" line whenever work is done.

**Last updated:** 2026-11-14 (crawl paused by user at 15:08 UTC)

## 1. Project in five lines

- IR course project (CADT, 5 members, 5 weeks): hybrid retrieval + RAG over official Cambodian
  government documents, Khmer and English. Corpus target: **at least 10,001 unique documents**.
- Stack: React + TS + Vite, FastAPI, PostgreSQL + pgvector + pg_trgm, Python ingestion/retrieval, Docker Compose.
- Academic core: fair comparison of lexical vs dense vs hybrid. RQ1 Khmer segmentation for BM25,
  RQ2 dense vs BM25, RQ3 hybrid (RRF), RQ4 reranking, RQ5 status/recency boost, RQ6 cross-lingual.
- Priority order if time is short: verified corpus > BM25/dense/hybrid > evaluation > demo.
  Drop order: table/form extraction, E8, reranker, E7.
- Python env: `Project/.venv` (Python 3.14). Has httpx, bs4, pymupdf, playwright (+ Chromium), numpy, openpyxl, pytest.

## 2. Where we are

**Phase 1 (collection) is built and about half run. Nothing after it exists yet.**

| Corpus figure (2026-11-14 snapshot) | Value |
|---|---|
| Document URLs found | ~18,400 |
| Downloaded | 7,502 |
| Still pending | 10,411 (mostly ODC 3,996, MRD 4,399, MPTC 840) |
| Retained after eligibility + dedup | **5,636** (target 10,001) |
| Original data on disk | 5.6 GB in `crawler/data/raw` |

**Immediate next action:** resume the crawl (section 5), then check the retained count in
`crawler/data/reports/corpus_report.md`. ODC's ~4,000 pending legal files are the main gap to 10,001.

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
- [~] Full crawl: 7,502 of ~18,400 downloaded, paused
- [ ] Reach 10,001 or over files that retained; if short, widen sources (see section 6)
- [ ] MoEYS: firewall rejected ~300 file downloads after 6 files. Retry slowly (`delay: 5` in sources.yaml, reset skipped rows) or drop
- [ ] Decide whether press releases count (`include_press` in `config/settings.yaml`; report shows both counts)
- [ ] Freeze corpus snapshot (week 5)

### Phase 2: Parsing and indexing (Indexing Engineer, weeks 1-4) — `[ ]`
- [ ] PostgreSQL schema v1 from section 11 (sources, documents, chunks, embeddings, doc_relations, assets, queries, relevance_judgments, search_runs) + Docker Compose
- [ ] Loader: `manifest.jsonl` -> `sources`; `relations.jsonl` -> `doc_relations`
- [ ] OCR for `needs_ocr` records (~650 scanned PDFs so far): Tesseract khm+eng, compare Surya/PaddleOCR; store `ocr_confidence` — 8.2
- [ ] Parsers: PDF text layer (PyMuPDF, keep pages), HTML, tables (pdfplumber/Camelot, droppable) — 8.2
- [ ] Khmer normalisation: NFC, ZWSP handling, Khmer digits -> ASCII in search text — 8.3 (reuse `classify.norm`)
- [ ] Tokenisation: khmer-nltk word segmentation AND character trigrams; English lowercase + light stemming — 8.4
- [ ] Chunking: structure-aware (ជំពូក / ផ្នែក / មាត្រា), parent/child, contextual header; fixed 512-token baseline for E6 — 8.5
- [ ] Metadata enrichment: doc number, issue/effective date, status; relations (amends, repeals, implements) — 8.6. Collection-time values are sparse outside ODC
- [ ] Indexes: BM25 (bm25s) word + trigram, pgvector HNSW (m=16, ef_construction=64), B-tree/GIN filters, pg_trgm — 9
- [ ] Embedding model choice on ~30 dev queries by Recall@20: BGE-M3, multilingual-e5-large, Qwen3-Embedding, EmbeddingGemma — 9.1

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

## 5. Crawler quick reference (run from `Project/crawler`)

```bash
../.venv/bin/python -m govkh_crawler status                                   # per-source progress
setsid nohup ./full_crawl.sh >> data/logs/full_crawl.out 2>&1 < /dev/null &   # resume full crawl (detached)
kill -TERM $(pgrep -f "bash ./full_crawl.sh"); kill -INT $(pgrep -f "python -m govkh_crawler")   # stop cleanly
../.venv/bin/python -m govkh_crawler process                                  # rebuild manifest + report (offline)
../.venv/bin/python report_xlsx.py . ../Docs/GovKH_Crawl_Status_Report.xlsx   # rebuild Excel report
../.venv/bin/python -m pytest                                                 # tests
```

- Details: `crawler/README.md`. Registry: `crawler/config/sources.yaml`. Settings: `crawler/config/settings.yaml`.
- Do not run `pkill -f "govkh_crawler run"` from a shell command that contains that text; it kills itself. Use the PIDs as above.
- Downstream input contract: `data/manifest/manifest.jsonl` (first 16 fields = `sources` table of section 11),
  originals at `data/<storage_uri>`, pre-extracted text at `data/<text_uri>`, `needs_ocr` flag, `relations.jsonl`.

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
