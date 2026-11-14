# GovKH Search: crawling pipeline

Collects original documents issued by Cambodian government institutions, with full provenance,
and produces the corpus manifest and audit report (proposal sections 6, 8.1 and the Data Engineer
deliverables). Parsing for retrieval, OCR, chunking and indexing are downstream stages that read
the manifest this pipeline writes.

## Quick start

```bash
cd Project/crawler
../.venv/bin/python -m pip install -e ".[dev]"      # or: uv pip install --python ../.venv/bin/python -e ".[dev]"
../.venv/bin/python -m pytest                        # offline tests against a local fake site

# 1. set `contact:` in config/settings.yaml (shown to site operators in the User-Agent)
python -m govkh_crawler audit                        # Week 1: reachability, robots.txt, estimated counts
python -m govkh_crawler pilot -n 200                 # Week 1: 200 documents per source, then the report
python -m govkh_crawler run                          # Week 2-3: full collection (safe to stop and rerun)
python -m govkh_crawler recrawl                      # weekly: new and changed documents only
```

Everything is resumable. Stop with Ctrl-C at any time and run the same command again.

For the full collection, `./full_crawl.sh` runs `run`, retries failures once, and builds the manifest.
Start it detached so it survives closing the terminal:

```bash
setsid nohup ./full_crawl.sh >> data/logs/full_crawl.out 2>&1 < /dev/null &
python -m govkh_crawler status                     # progress per source, any time
kill -INT $(pgrep -f "m govkh_crawler run")         # stop cleanly; rerun the script to resume
```

`run` discovers and downloads independently per source, so a slow site never holds up the others.

## Commands

| Command | What it does |
|---|---|
| `sources` | Lists the source registry. |
| `audit` | Checks every registered site (robots.txt, homepage, estimated documents) and writes `data/reports/source_audit.md` and `.csv`. |
| `discover` | Finds document URLs through each source's adapters and adds them to the frontier. |
| `fetch` | Downloads pending URLs. One request at a time per host, spaced by `default_delay`. |
| `process` | Builds records, deduplicates, and writes the manifest and corpus report. Offline; rerun it after changing rules. |
| `run` | `discover` + `fetch` + `process`. |
| `pilot -n 200` | A bounded `run` per source for the feasibility pilot. |
| `recrawl` | Re-reads listings and APIs, re-checks documents older than `recrawl_days` with conditional requests, writes `manifest/changes.jsonl`. |
| `retry-failed` | Puts failed URLs back in the queue. |
| `status` | Frontier and retained counts per source. |

`-s <id>` limits any command to one source (repeatable). `--data-dir` and `--config-dir` go before the command.

## Output (`data/`, not committed)

| Path | Content |
|---|---|
| `state.sqlite` | The checkpoint: frontier, fetch log, cursors, records, file versions. |
| `raw/ab/<sha256>.<ext>` | Original files, content-addressed, so identical files are stored once. |
| `text/ab/<sha256>.txt` | Text extracted at collection time (HTML main text, PDF text layer). A convenience for the indexing stage, which may re-parse. |
| `manifest/manifest.jsonl`, `.csv` | One line per retained document. |
| `manifest/excluded.jsonl` | Rejected records and duplicates, each with its reason. |
| `manifest/relations.jsonl` | `translation_of`, plus `amends` / `implements` / `references` where the source catalogue states them. |
| `manifest/changes.jsonl` | Added, changed and removed record IDs since the previous export: what to re-index. |
| `reports/corpus_report.md`, `.json` | Funnel counts, acceptance criteria, per-source pilot metrics. |
| `reports/source_audit.md`, `.csv` | The source feasibility audit. |
| `logs/crawl-YYYYMMDD.log` | Full log. |

### Manifest fields

The first sixteen match the `sources` table of the proposal (section 11): `source_id` (stable internal
ID, `gkh-<site>-<hash of URL>`), `canonical_url`, `alternate_urls`, `institution`, `doc_type`, `doc_number`,
`title`, `language` (`km`, `en`, `mixed`), `issue_date`, `effective_date`, `status`, `collected_at`,
`checksum` (SHA-256), `file_type`, `reuse_status`, `storage_uri`.

Extra fields: `site_id` (registry entry), `institution_km`, `doc_subtype` (law, sub_decree, prakas,
notification, ...), `doc_type_source` and `issue_date_source` (where each value came from: catalogue,
title rule, publication date, source default), `bytes`, `page_count`, `text_chars`, `text_uri`,
`is_scanned`, `needs_ocr`, `is_press`, `landing_url`, `group_key`, `content_hash`, `assets`
(page images kept for OCR), `extra`.

## How it decides

**Official-source rule.** Only hosts in `config/sources.yaml` are requested. Social media hosts are
always refused. A redirect that leaves the whitelist is discarded.

**Politeness.** The crawler identifies itself honestly, reads robots.txt (wildcards, longest match,
Crawl-delay, Content-Signal) and obeys it, sends about one request per second per host, retries with
exponential backoff and honours `Retry-After`. A host that fails eight times in a row is paused for
the run. A site that answers 403 to the crawler is recorded as not crawlable; nothing is bypassed.

**Counting rule.** One original document is one record. A Khmer original and its English version are
two records linked by `translation_of`. A short page that only introduces its attachment is a landing
page: the attachment is the record and inherits the page's title and date. Images are assets, not records.

**Eligibility.** A record is rejected, with a reason, when it is a landing page, has almost no content,
is in another language, is a draft with no promulgation date or number, has a title that indicates a
list of named individuals, or cannot be opened.

**Deduplication.** Exact (SHA-256), then identical text after normalisation, then near duplicates by
MinHash over character 5-grams at 0.9 similarity. Records with different document numbers, issue dates
or languages are never merged, so amended versions and same-template notices stay separate. The copy
from the higher-`priority` source is kept and the others become `alternate_urls`.

**Incremental crawl.** WordPress and CKAN are asked only for items modified since the last full pass;
sitemaps use `<lastmod>`; files are re-checked with `If-None-Match` / `If-Modified-Since`. A change is
reported only when the document's text (or, for files without text, its bytes) differs.

## Adding a source

Add an entry to `config/sources.yaml` and choose an adapter:

| Adapter | Use when | Key options |
|---|---|---|
| `wordpress` | The site exposes `/wp-json/wp/v2` | `apis`, `types`, `media`, `categories` |
| `sitemap` | The site has an XML sitemap | `urls`, `include`, `exclude` |
| `ckan` | A CKAN catalogue | `api`, `fq`, `dataset_url` |
| `crawl` | Listing pages | `start_urls`, `allow`, `deny`, `doc_page_patterns`, `pagination`, `max_depth`, `max_pages` |
| `moeys` | The MoEYS document API | `api`, `file_base` |

Set `render: true` on a source whose pages are built by JavaScript: listing and document pages are then
loaded in headless Chromium (Playwright) and the rendered HTML is parsed. Files are still downloaded
directly. The browser sends the crawler's own User-Agent; it does not disguise itself or solve challenges.

**Google Drive and Docs.** When an official page links to or embeds a public Drive/Docs file, the file is
downloaded through its direct-download URL and recorded with the official page as `landing_url`. Drive's
robots.txt disallows that download endpoint for crawlers; these files are fetched one at a time, only when
an official page embeds them. Set `fetch_embedded_files: false` in settings.yaml to stop. Drive folders are
not expanded.

Then run `audit -s <id>` and `pilot -s <id> -n 30`, and read the per-source row of the corpus report.

## Weekly re-crawl with cron

```cron
0 2 * * 0  cd /path/to/Project/crawler && ../.venv/bin/python -m govkh_crawler recrawl >> data/logs/cron.log 2>&1
```

## Known limitations

- MEF, NBC, MoI, TRC and MFAIC block declared crawlers, including a headless browser (bot challenge or
  WAF rejection). They are not bypassed. Their instruments reach the corpus through ODC where ODC holds them.
- NSSF has almost no content even when rendered; MLVT's document sections answered HTTP 500.
- Where the ODC catalogue does not name the issuing agency, the record's institution reads
  "Royal Government of Cambodia (issuing body not recorded by the archive)".
- Document number and issue date are best-effort at collection time. They are complete for ODC records
  and sparse elsewhere; metadata enrichment (proposal 8.6) fills the rest after parsing and OCR.
- Titles of PDFs linked with a generic label ("download") fall back to the file name.
- Legacy `.doc` / `.xls` files are stored but their text is not extracted here.
