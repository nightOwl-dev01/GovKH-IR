# Contributing to GovKH Search

This is a five-person course project. These rules keep the corpus, the indexes and the experiment
results reproducible while several people work at once.

## Before you start

1. Read [PROGRESS.md](PROGRESS.md). It lists every task, who owns each phase, and the decisions already made.
2. Set up the environment as described in [README.md](README.md).
3. Pick a task from your phase. If you take a task from another phase, tell its owner first.

| Phase | Owner role | Code |
|---|---|---|
| 1 Collection | Data Engineer | `crawler/` |
| 2 Parsing and indexing | Indexing Engineer | `backend/govkh/` (ingest, enrich, chunk, lexical, dense) |
| 3 Retrieval and RAG | Retrieval Engineer | `backend/govkh/` |
| 4 Evaluation | Evaluation Engineer | `evaluation/` |
| 5 Application | Application Engineer | API and `frontend/` |

## Workflow

- Work on a branch named `<phase>/<short-topic>`, for example `retrieval/rrf-fusion`. Do not commit to `main` directly.
- Keep one topic per pull request. Another member reviews it before it is merged.
- Write commit messages in the imperative and say why: `Send PDFs with a broken Khmer text layer to OCR`.
- When a task is finished, tick its box in `PROGRESS.md` and change the "Last updated" line in the same pull request.

## What must not be committed

- `crawler/data/` and `backend/data/`: downloaded documents, extracted text, OCR output, indexes
- `.venv/`, `.local/` (the development database), `node_modules/`
- API keys or any `.env` file

These are already in `.gitignore`. If you need to share data, share the manifest and the commands that rebuild it.

## Rules for the crawler

- Request only hosts listed in `crawler/config/sources.yaml`. A new source needs an audit entry first
  (`python -m govkh_crawler audit -s <id>`).
- Obey robots.txt and keep about one request per second per host.
- Never bypass bot protection, logins or CAPTCHAs. If a site blocks the crawler, record it as not crawlable.
- Do not collect documents that list named private individuals.

## Rules for code

- Python 3.14, type hints on public functions, lines up to about 120 characters.
- Every pipeline step must be incremental and safe to rerun. Key caches by file checksum.
- Text processing must be identical for documents and queries. Use the functions in `backend/govkh/text.py`;
  do not write a second normaliser or tokeniser.
- Keep the original text for display. Only the search text is normalised.
- New settings go in `backend/govkh/config.py` with an environment-variable override.
- Add or update a test for behaviour you change. Run the tests before you open a pull request:

```bash
cd crawler && ../.venv/bin/python -m pytest
```

## Rules for evaluation

These protect the fairness of the comparison, which is the academic core of the project.

- Tune parameters on the development queries only. Freeze them before the first run on the test queries.
- All methods are evaluated on the same queries, the same corpus snapshot and the same relevance judgments.
- Judgments come from pooling the top 20 results of every method, so no method is favoured.
- Record the model name and version, index settings and hardware with every result.
- Report only numbers from experiments that were actually run. Do not fill in expected values.
- Labels written by a language model are marked `labeled_by = model`. A label counts as human only
  after a team member has checked it.

## Reporting a problem

Open an issue with the command you ran, what you expected, what happened, and the relevant lines from
`crawler/data/logs/` or `backend/data/logs/`. For a wrong search result, include the query, the method
and the `source_id` of the document.
