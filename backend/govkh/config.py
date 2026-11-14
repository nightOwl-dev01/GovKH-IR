"""Paths and settings. Every value can be overridden with an environment variable."""
from __future__ import annotations

import os
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent
CRAWL_DATA = Path(os.environ.get("GOVKH_CRAWL_DATA", PROJECT / "crawler" / "data"))
DATA = Path(os.environ.get("GOVKH_DATA", BACKEND / "data"))
OCR_DIR = DATA / "ocr"
INDEX_DIR = DATA / "index"
TESSDATA = Path(os.environ.get("GOVKH_TESSDATA", DATA / "tessdata"))
EVAL_DIR = PROJECT / "evaluation"

DSN = os.environ.get("GOVKH_DSN", "postgresql://govkh@127.0.0.1:5433/govkh")

# Embedding model used for the whole index (proposal 9.1). Chosen by `python -m govkh.evalkit.model_choice`.
EMBED_MODEL = os.environ.get("GOVKH_EMBED_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.environ.get("GOVKH_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
LLM_MODEL = os.environ.get("GOVKH_LLM_MODEL", "gpt-4.1-mini")

# Chunking (proposal 8.5)
CHILD_TARGET_TOKENS = 400
CHILD_MAX_TOKENS = 500
PARENT_MAX_TOKENS = 1600
FIXED_TOKENS = 512
FIXED_OVERLAP = 64  # 12.5%

# OCR (proposal 8.2)
OCR_DPI = 200
OCR_MAX_PAGES = 40  # pages OCRed per scanned document; the rest is recorded as not processed

# Ranking (proposal 10.3)
RRF_K = 60
CANDIDATES = 100
RERANK_TOP = 50
MAX_CHUNKS_PER_DOC = 3
CONFIG_VERSION = "v1"

for d in (DATA, OCR_DIR, INDEX_DIR):
    d.mkdir(parents=True, exist_ok=True)
