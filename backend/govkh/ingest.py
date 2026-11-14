"""Manifest loader, OCR and parsers (proposal 8.2). Every step is incremental and keyed by checksum."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from psycopg.types.json import Jsonb

from . import config, db
from .text import display_clean, khmer_quality

QUALITY_MIN = 0.95  # below this a PDF text layer is treated as broken (legacy font) and the file is OCRed
MAX_PDF_PAGES = 400


def _date(v):
    try:
        return date.fromisoformat(v[:10]) if v else None
    except ValueError:
        return None


def manifest_rows():
    with open(config.CRAWL_DATA / "manifest" / "manifest.jsonl", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


# -- loader ----------------------------------------------------------------
def load() -> dict:
    """manifest.jsonl -> sources, relations.jsonl -> doc_relations. Removes sources that left the manifest."""
    rows = list(manifest_rows())
    with db.connect() as con:
        cur = con.cursor()
        cur.execute("CREATE TEMP TABLE keep (source_id text PRIMARY KEY) ON COMMIT DROP")
        cur.executemany("INSERT INTO keep VALUES (%s)", [(r["source_id"],) for r in rows])
        removed = cur.execute("DELETE FROM sources WHERE source_id NOT IN (SELECT source_id FROM keep)").rowcount
        cur.executemany(
            """INSERT INTO sources (source_id, canonical_url, alternate_urls, institution, doc_type, doc_number, title,
                   language, issue_date, effective_date, status, collected_at, checksum, file_type, reuse_status,
                   storage_uri, site_id, institution_km, doc_subtype, is_press, needs_ocr, text_uri, landing_url, meta_source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (source_id) DO UPDATE SET
                   canonical_url=EXCLUDED.canonical_url, alternate_urls=EXCLUDED.alternate_urls,
                   institution=EXCLUDED.institution, title=EXCLUDED.title, language=EXCLUDED.language,
                   collected_at=EXCLUDED.collected_at, checksum=EXCLUDED.checksum, file_type=EXCLUDED.file_type,
                   storage_uri=EXCLUDED.storage_uri, site_id=EXCLUDED.site_id, institution_km=EXCLUDED.institution_km,
                   is_press=EXCLUDED.is_press, needs_ocr=EXCLUDED.needs_ocr, text_uri=EXCLUDED.text_uri,
                   landing_url=EXCLUDED.landing_url""",
            [(
                r["source_id"], r["canonical_url"], Jsonb(r.get("alternate_urls") or []), r["institution"], r["doc_type"],
                r.get("doc_number") or "", r.get("title") or "", r["language"], _date(r.get("issue_date")),
                _date(r.get("effective_date")), r.get("status") if r.get("status") in ("in_force", "amended", "repealed") else "unknown",
                r["collected_at"], r["checksum"], r["file_type"], r.get("reuse_status") or "unclear", r["storage_uri"],
                r["site_id"], r.get("institution_km") or "", r.get("doc_subtype") or "other", bool(r.get("is_press")),
                bool(r.get("needs_ocr")), r.get("text_uri") or "", r.get("landing_url") or "",
                Jsonb({"doc_type": r.get("doc_type_source") or "", "issue_date": r.get("issue_date_source") or "",
                       "doc_number": "collection" if r.get("doc_number") else ""}),
            ) for r in rows],
        )
        rel = 0
        path = config.CRAWL_DATA / "manifest" / "relations.jsonl"
        if path.exists():
            ids = {r["source_id"] for r in rows}
            rels = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            rels = [x for x in rels if x["from_source_id"] in ids and x["to_source_id"] in ids]
            cur.executemany(
                "INSERT INTO doc_relations VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                [(x["from_source_id"], x["to_source_id"], x["relation"], x.get("evidence") or "") for x in rels],
            )
            rel = len(rels)
    return {"sources": len(rows), "removed": removed, "relations": rel}


# -- OCR -------------------------------------------------------------------
def _tesseract(image: Path) -> tuple[str, float]:
    """Text and mean word confidence (0-100) of one page image."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "o"
        subprocess.run(
            ["tesseract", str(image), str(base), "--tessdata-dir", str(config.TESSDATA), "-l", "khm+eng", "--psm", "3", "-c", "tessedit_create_txt=1", "-c", "tessedit_create_tsv=1"],
            capture_output=True, timeout=300, env={**os.environ, "OMP_THREAD_LIMIT": "1"}, check=True,
        )
        text = base.with_suffix(".txt").read_text(encoding="utf-8", errors="replace")
        confs = []
        for line in base.with_suffix(".tsv").read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
            cols = line.split("\t")
            if len(cols) >= 12 and cols[11].strip():
                try:
                    c = float(cols[10])
                except ValueError:
                    continue
                if c >= 0:
                    confs.append(c)
    return text, (sum(confs) / len(confs) if confs else 0.0)


def ocr_file(checksum: str, storage_uri: str, file_type: str) -> dict:
    """OCR one original into OCR_DIR/<checksum>.json. Returns the cached result if present."""
    out = config.OCR_DIR / f"{checksum}.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    src = config.CRAWL_DATA / storage_uri
    res = {"engine": "tesseract khm+eng", "dpi": config.OCR_DPI, "pages": [], "total_pages": 0, "error": ""}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            if file_type == "pdf":
                import pymupdf
                with pymupdf.open(src) as pdf:
                    res["total_pages"] = pdf.page_count
                    for i in range(min(pdf.page_count, config.OCR_MAX_PAGES)):
                        img = Path(tmp) / f"{i}.png"
                        pdf[i].get_pixmap(dpi=config.OCR_DPI, colorspace=pymupdf.csGRAY).save(img)
                        text, conf = _tesseract(img)
                        res["pages"].append({"n": i + 1, "text": text, "conf": round(conf, 1)})
            else:  # an image
                res["total_pages"] = 1
                text, conf = _tesseract(src)
                res["pages"].append({"n": 1, "text": text, "conf": round(conf, 1)})
    except Exception as e:  # damaged file, tesseract failure, timeout
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    out.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return res


def ocr(workers: int = 12, limit: int | None = None) -> dict:
    """OCR every document whose parse is waiting for it."""
    with db.connect() as con:
        todo = con.execute(
            """SELECT s.checksum, s.storage_uri, s.file_type FROM documents d JOIN sources s USING (source_id)
               WHERE d.parse_status = 'pending_ocr' AND s.file_type IN ('pdf','jpg','png') ORDER BY s.source_id"""
        ).fetchall()
    todo = [t for t in todo if not (config.OCR_DIR / f"{t['checksum']}.json").exists()][:limit]
    done = failed = 0
    with ProcessPoolExecutor(workers) as pool:
        futs = [pool.submit(ocr_file, t["checksum"], t["storage_uri"], t["file_type"]) for t in todo]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            failed += bool(r["error"])
            done += 1
            if i % 25 == 0:
                print(f"ocr {i}/{len(todo)} (failed {failed})", flush=True)
    return {"ocr_done": done, "ocr_failed": failed}


# -- parsers -----------------------------------------------------------------
def _pdf_pages(path: Path) -> tuple[list[str], int]:
    import pymupdf
    with pymupdf.open(path) as pdf:
        if pdf.needs_pass:
            raise ValueError("encrypted")
        n = pdf.page_count
        return [pdf[i].get_text("text") for i in range(min(n, MAX_PDF_PAGES))], n


def parse_one(src: dict) -> dict:
    """Text of one source. Pages are joined with form feeds so page numbers survive chunking."""
    out = {"source_id": src["source_id"], "checksum": src["checksum"], "text": "", "page_count": None,
           "status": "ok", "ocr_used": False, "ocr_conf": None, "quality": None}
    ft = src["file_type"]
    try:
        pages: list[str] | None = None
        if ft == "pdf":
            pages, out["page_count"] = _pdf_pages(config.CRAWL_DATA / src["storage_uri"])
            text = "\f".join(display_clean(p) for p in pages)
            q = khmer_quality(text)
            letters = sum(ch.isalnum() for ch in text)
            scanned = letters / max(1, len(pages)) < 50
            if scanned or (q is not None and q < QUALITY_MIN):
                cache = config.OCR_DIR / f"{src['checksum']}.json"
                if not cache.exists():
                    out["status"] = "pending_ocr"
                    out["quality"] = q
                    return out
                res = json.loads(cache.read_text(encoding="utf-8"))
                good = [p for p in res["pages"] if p["text"].strip()]
                if res["error"] and not good:
                    out["status"] = "error:ocr"
                    return out
                text = "\f".join(display_clean(p["text"]) for p in res["pages"])
                words = [(len(p["text"]), p["conf"]) for p in good]
                out["ocr_used"] = True
                out["ocr_conf"] = sum(n * c for n, c in words) / max(1, sum(n for n, _ in words))
                out["status"] = "ocr"
                q = khmer_quality(text)
            out["quality"] = q
        elif src["text_uri"]:
            text = display_clean((config.CRAWL_DATA / src["text_uri"]).read_text(encoding="utf-8", errors="replace"))
            out["quality"] = khmer_quality(text)
        else:
            out["status"] = "unsupported"  # legacy .doc/.xls, or an HTML page that is only images
            return out
        out["text"] = text.replace("\x00", "")
        if len(re.sub(r"\s", "", text)) < 40:
            out["status"] = "empty"
    except Exception as e:
        out["status"] = f"error:{type(e).__name__}"
    return out


def parse(workers: int = 12) -> dict:
    """(Re)parse sources that have no document row, a changed file, or an OCR result that has since arrived."""
    with db.connect() as con:
        todo = con.execute(
            """SELECT s.source_id, s.checksum, s.file_type, s.storage_uri, s.text_uri, d.parse_status
               FROM sources s LEFT JOIN documents d USING (source_id)
               WHERE d.source_id IS NULL OR d.checksum <> s.checksum OR d.parse_status = 'pending_ocr'"""
        ).fetchall()
    todo = [t for t in todo if t["parse_status"] != "pending_ocr" or (config.OCR_DIR / f"{t['checksum']}.json").exists()]
    counts: dict[str, int] = {}
    with ProcessPoolExecutor(workers) as pool, db.connect() as con:
        for i, r in enumerate(pool.map(parse_one, todo, chunksize=8), 1):
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            con.execute(
                """INSERT INTO documents (source_id, checksum, extracted_text, page_count, parse_status, ocr_used, ocr_confidence, text_quality)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (source_id) DO UPDATE SET checksum=EXCLUDED.checksum, extracted_text=EXCLUDED.extracted_text,
                       page_count=EXCLUDED.page_count, parse_status=EXCLUDED.parse_status, ocr_used=EXCLUDED.ocr_used,
                       ocr_confidence=EXCLUDED.ocr_confidence, text_quality=EXCLUDED.text_quality""",
                (r["source_id"], r["checksum"], r["text"], r["page_count"], r["status"], r["ocr_used"], r["ocr_conf"], r["quality"]),
            )
            # a re-parsed document gets new chunks
            con.execute("DELETE FROM chunks WHERE document_id = (SELECT document_id FROM documents WHERE source_id = %s)", (r["source_id"],))
            if i % 500 == 0:
                con.commit()
                print(f"parse {i}/{len(todo)}", flush=True)
    return counts


# -- tables (proposal 8.2; droppable) ---------------------------------------------
def tables(limit: int | None = None) -> dict:
    """Extract tables from fee schedules and forms with pdfplumber into `assets` (rows + a text version)."""
    import pdfplumber
    with db.connect() as con:
        todo = con.execute(
            """SELECT d.document_id, s.storage_uri FROM documents d JOIN sources s USING (source_id)
               WHERE s.file_type = 'pdf' AND d.parse_status = 'ok' AND d.page_count <= 30
                 AND (s.doc_type = 'form_fee' OR s.doc_subtype IN ('fee_schedule', 'procedure'))
                 AND NOT EXISTS (SELECT 1 FROM assets a WHERE a.document_id = d.document_id AND a.asset_type = 'table')
               ORDER BY d.document_id"""
        ).fetchall()[:limit]
        n = docs = 0
        for t in todo:
            try:
                with pdfplumber.open(config.CRAWL_DATA / t["storage_uri"]) as pdf:
                    found = False
                    for pno, page in enumerate(pdf.pages, 1):
                        for tb in page.extract_tables():
                            rows = [[(c or "").strip() for c in row] for row in tb if any(row)]
                            if len(rows) < 2 or max(len(r) for r in rows) < 2:
                                continue
                            txt = "\n".join(" | ".join(r) for r in rows)
                            con.execute(
                                "INSERT INTO assets (document_id, asset_type, page_number, extracted_text, rows) VALUES (%s,'table',%s,%s,%s)",
                                (t["document_id"], pno, txt.replace("\x00", ""), Jsonb(rows)),
                            )
                            n += 1
                            found = True
                    docs += found
            except Exception:
                continue
    return {"tables": n, "documents_with_tables": docs, "documents_scanned": len(todo)}
