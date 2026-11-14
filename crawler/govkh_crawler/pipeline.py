"""The collection pipeline: discover -> fetch -> process (records). Dedup and export live elsewhere.

Every stage is idempotent and resumable: all progress is in the SQLite state database, so a
crash or Ctrl-C loses at most the request in flight.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import classify as C
from .config import ROOT, Settings, Source
from .db import DB, now_iso
from .dedup import minhash, text_hash
from .discovery import ADAPTERS, Candidate
from .http import FetchResult, HostBlocked, PoliteClient
from .textextract import DOC_TYPES, IMAGE_TYPES, Extracted, HtmlDoc, extract, parse_html, sniff_path
from .urlnorm import DOC_EXT, IMG_EXT, SKIP_EXT, extension, filename, is_drive_file, is_social
from urllib.parse import unquote, urlsplit

log = logging.getLogger("govkh.pipeline")
INLINE = "wordpress:inline"


def record_id(source_id: str, url: str) -> str:
    """Stable internal ID: the same URL always maps to the same record."""
    return f"gkh-{source_id}-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}"


class Pipeline:
    def __init__(self, settings: Settings, sources: dict[str, Source], base_dir: Path = ROOT):
        self.s = settings
        self.sources = sources
        self.data = settings.data_path(base_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.db = DB(self.data / "state.sqlite")
        self.stop = threading.Event()
        self.client = PoliteClient(settings, self.data / "tmp", self.stop)
        self.client.on_fetch = self._log_fetch
        self._tl = threading.local()
        self._re_cache: dict[str, Any] = {}

    def close(self) -> None:
        self.client.close()
        self.db.close()

    # ------------------------------------------------------------------ util
    def selected(self, ids: list[str] | None) -> list[Source]:
        if ids:
            missing = [i for i in ids if i not in self.sources]
            if missing:
                raise KeyError(f"unknown source id(s): {', '.join(missing)}")
            return [self.sources[i] for i in ids]
        return [s for s in self.sources.values() if s.enabled]

    def _log_fetch(self, res: FetchResult) -> None:
        self.db.x(
            "INSERT INTO fetch_log VALUES (?,?,?,?,?,?,?,?)",
            (now_iso(), getattr(self._tl, "source", ""), res.url, res.status, res.size, res.elapsed_ms, res.attempts, res.error),
        )

    def _parallel(self, fn, sources: list[Source], *args) -> None:
        threads = [threading.Thread(target=self._guarded, args=(fn, src, *args), name=src.id, daemon=True) for src in sources]
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                for t in threads:
                    t.join(0.5)
        except KeyboardInterrupt:
            log.warning("interrupt received: finishing requests in flight, state is saved")
            self.stop.set()
            for t in threads:
                t.join()

    def _guarded(self, fn, src: Source, *args) -> None:
        self._tl.source = src.id
        try:
            fn(src, *args)
        except HostBlocked as e:
            log.warning("[%s] stopped: host paused (%s)", src.id, e)
        except Exception:
            log.exception("[%s] stage crashed; other sources continue", src.id)
        finally:
            self.client.renderers.close_current()

    def _embedded(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return self.s.fetch_embedded_files and any(host == h or host.endswith("." + h) for h in self.s.embedded_file_hosts)

    def _allowed(self, src: Source, url: str) -> bool:
        """Source whitelist, plus files embedded from Drive/Docs (only ever queued from official pages)."""
        return src.allows(url) or self._embedded(url)

    def _blob_path(self, sha: str, ftype: str) -> Path:
        return self.data / "raw" / sha[:2] / f"{sha}.{ftype}"

    def _store_blob(self, tmp: Path | None, data: bytes | None, sha: str, ftype: str) -> str:
        dest = self._blob_path(sha, ftype)
        if dest.exists():
            if tmp:
                tmp.unlink(missing_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if tmp:
                tmp.replace(dest)
            else:
                part = dest.with_suffix(".part")
                part.write_bytes(data or b"")
                part.replace(dest)
        uri = dest.relative_to(self.data).as_posix()
        self.db.x(
            "INSERT OR IGNORE INTO blobs(sha256, bytes, file_type, storage_uri, first_seen) VALUES (?,?,?,?,?)",
            (sha, dest.stat().st_size, ftype, uri, now_iso()),
        )
        return uri

    # ------------------------------------------------------------- discovery
    def discover(self, source_ids: list[str] | None = None, limit: int | None = None) -> None:
        self._parallel(self._discover_source, self.selected(source_ids), limit)

    def _discover_source(self, src: Source, limit: int | None) -> None:
        for cfg in src.adapters:
            kind = cfg.get("type")
            if kind not in ADAPTERS:
                log.error("[%s] unknown adapter type %r", src.id, kind)
                continue
            adapter = ADAPTERS[kind](self.s, self.db, self.client, src, cfg)
            new = seen = 0
            for cand in adapter.discover(limit * 2 if limit else None):
                if self.stop.is_set():
                    break
                seen += 1
                if is_social(cand.url) or src.excluded(cand.url):
                    continue
                if cand.inline is not None:
                    new += self._ingest_inline(src, cand)
                else:
                    new += self.db.enqueue(cand.url, src.id, cand.kind, via=kind, parent_url=cand.parent_url, hints=cand.hints)
                if limit and new >= limit * 2:
                    break
            log.info("[%s] %s discovery: %d candidates, %d new", src.id, kind, seen, new)

    def _ingest_inline(self, src: Source, cand: Candidate) -> bool:
        """Store a document body that the discovery API already returned."""
        doc = parse_html(cand.inline or b"", cand.url)
        has_files = any(in_main and (extension(u) in DOC_EXT or src.is_file_url(u)) for u, _, in_main in doc.links)
        if len(doc.text) < self.s.thin_text_chars and not has_files and not doc.images:
            # The API body is empty (theme renders custom fields): fetch the real page instead.
            return self.db.enqueue(cand.url, src.id, "page", via="wordpress", hints=cand.hints)
        sha = hashlib.sha256(cand.inline).hexdigest()
        new = self.db.enqueue(cand.url, src.id, "page", via=INLINE, hints=cand.hints)
        row = self.db.one("SELECT * FROM urls WHERE url=?", (cand.url,))
        if row["sha256"] == sha and row["state"] == "done":
            return new
        self._store_blob(None, cand.inline, sha, "html")
        self._mark_stored(row, sha, "html", len(cand.inline), doc, via=INLINE)
        self._expand(src, self.db.one("SELECT * FROM urls WHERE url=?", (cand.url,)), doc)
        return new

    def run(self, source_ids: list[str] | None = None, limit: int | None = None) -> None:
        """Discover then fetch, independently per source: a slow API never holds up other sites."""
        self._parallel(self._run_source, self.selected(source_ids), limit)
        self._record_blocked_hosts()

    def _run_source(self, src: Source, limit: int | None) -> None:
        try:
            self._discover_source(src, limit)
        except HostBlocked as e:
            log.warning("[%s] discovery stopped: host paused (%s)", src.id, e)
        except Exception:
            log.exception("[%s] discovery crashed; fetching what was found", src.id)
        if not self.stop.is_set():
            self._fetch_source(src, limit)

    def _record_blocked_hosts(self) -> None:
        for host, why in self.client.blocked_hosts().items():
            self.db.x(
                "INSERT INTO host_status(host, blocked, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(host) DO UPDATE SET blocked=excluded.blocked, updated_at=excluded.updated_at",
                (host, why, now_iso()),
            )

    # ----------------------------------------------------------------- fetch
    def fetch(self, source_ids: list[str] | None = None, limit: int | None = None) -> None:
        self._parallel(self._fetch_source, self.selected(source_ids), limit)
        self._record_blocked_hosts()

    def _fetch_source(self, src: Source, limit: int | None) -> None:
        stored = 0
        cap = src.max_docs
        last_report = time.monotonic()
        while not self.stop.is_set():
            if cap and self.db.count("source_id=? AND state='done' AND kind IN ('page','file')", (src.id,)) >= cap:
                log.info("[%s] reached max_docs=%d", src.id, cap)
                return
            rows = self.db.next_pending(src.id, 20)
            if not rows:
                break
            for row in rows:
                if self.stop.is_set() or (limit and stored >= limit):
                    break
                stored += self._fetch_one(src, row)
                if time.monotonic() - last_report > 30:
                    last_report = time.monotonic()
                    log.info("[%s] stored %d this run, %d pending", src.id, stored,
                             self.db.count("source_id=? AND state='pending'", (src.id,)))
            if limit and stored >= limit:
                break
        log.info("[%s] fetch finished: %d documents stored this run", src.id, stored)

    def _fetch_one(self, src: Source, row) -> int:
        """Download one frontier URL. Returns 1 when a document (page/file) body was stored."""
        url, kind = row["url"], row["kind"]
        if is_social(url) or not self._allowed(src, url):
            self.db.update_url(url, state="offlist", last_error="not on the official-source whitelist")
            return 0
        if src.excluded(url) or extension(url) in SKIP_EXT:
            self.db.update_url(url, state="skipped", last_error="excluded by rule")
            return 0
        have_blob = bool(row["sha256"] and row["file_type"] and self._blob_path(row["sha256"], row["file_type"]).exists())
        res = self.client.fetch(
            url,
            etag=row["etag"] if have_blob else None,
            last_modified=row["last_modified"] if have_blob else None,
            delay=src.delay,
            verify=src.verify_tls,
            check_robots=not self._embedded(url),
            render=src.render and kind in ("listing", "page"),
        )
        now = now_iso()
        if res.error in ("robots_disallowed",):
            self.db.update_url(url, state="robots_blocked", last_error=res.error, checked_at=now)
            return 0
        if res.error == "too_large":
            self.db.update_url(url, state="skipped", last_error=f"larger than {self.s.max_bytes} bytes", checked_at=now)
            return 0
        if res.error == "interrupted":
            return 0
        if res.status == 304:
            self.db.update_url(url, state="done", http_status=304, checked_at=now, last_error=None)
            return 0
        if res.status in (404, 410):
            res.discard()
            self.db.update_url(url, state="gone", http_status=res.status, checked_at=now, last_error=f"http_{res.status}")
            return 0
        if not res.ok:
            res.discard()
            attempts = row["attempts"] + 1
            final = attempts >= self.s.max_attempts
            self.db.update_url(
                url,
                state="failed" if final else "pending",
                attempts=attempts,
                next_attempt_at=time.time() + min(6 * 3600, 300 * 2**attempts),
                http_status=res.status or None,
                last_error=res.error or f"http_{res.status}",
                checked_at=now,
            )
            return 0
        if res.final_url and (is_social(res.final_url) or not self._allowed(src, res.final_url)):
            res.discard()
            self.db.update_url(url, state="offlist", last_error=f"redirected off the whitelist: {res.final_url}", checked_at=now)
            return 0
        return self._handle_body(src, row, res)

    def _handle_body(self, src: Source, row, res: FetchResult) -> int:
        url, kind, now = row["url"], row["kind"], now_iso()
        ftype = sniff_path(res.path, res.final_url or url, res.headers.get("content-type", ""))
        common = dict(
            http_status=200, final_url=res.final_url if res.final_url != url else None,
            etag=res.headers.get("etag"), last_modified=res.headers.get("last-modified"),
            checked_at=now,
        )
        ok = dict(common, attempts=0)
        if ftype == "html":
            if kind in ("file", "asset"):
                res.discard()  # a "download" that answers with a web page is an error page or a login wall
                self.db.update_url(url, state="skipped", last_error="expected a file, got HTML", **common)
                return 0
            doc = parse_html(res.path.read_bytes(), res.final_url or url)
            self._expand(src, row, doc)
            if kind == "listing":
                res.discard()
                self.db.update_url(url, state="done", file_type="html", fetched_at=now, last_error=None, **ok)
                return 0
            self._store_blob(res.path, None, res.sha256, "html")
            self._mark_stored(row, res.sha256, "html", res.size, doc, **ok)
            return 1
        if kind == "asset" or (ftype in IMAGE_TYPES and kind in ("file", "page")):
            if ftype not in IMAGE_TYPES or res.size < 15_000:  # icons and spacers are not notices
                res.discard()
                self.db.update_url(url, state="skipped", kind="asset", last_error=f"not a content image ({ftype}, {res.size} B)", **common)
                return 0
            self._store_blob(res.path, None, res.sha256, ftype)
            self._mark_stored(row, res.sha256, ftype, res.size, None, kind="asset", **ok)
            return 0
        if ftype in DOC_TYPES:
            self._store_blob(res.path, None, res.sha256, ftype)
            fname = _disposition_filename(res.headers.get("content-disposition", ""))
            meta = json.dumps({"filename": fname}, ensure_ascii=False) if fname else None
            self._mark_stored(row, res.sha256, ftype, res.size, None, kind="file", page_meta=meta, **ok)
            return 1
        res.discard()
        self.db.update_url(url, state="skipped", last_error=f"unsupported file type: {ftype}", **common)
        return 0

    def _mark_stored(self, row, sha: str, ftype: str, size: int, doc: HtmlDoc | None, **cols: Any) -> None:
        now = now_iso()
        changed = row["sha256"] != sha
        meta = None
        if doc is not None:
            meta = json.dumps({"title": doc.title, "titles": doc.titles, "text_chars": len(doc.text), "lang": doc.lang}, ensure_ascii=False)
        vals: dict[str, Any] = dict(
            state="done", sha256=sha, file_type=ftype, bytes=size, page_meta=meta,
            fetched_at=now, checked_at=now, last_error=None,
        )
        if changed:
            vals["changed_at"] = now
        vals.update(cols)
        self.db.update_url(row["url"], **vals)
        self.db.x("INSERT OR IGNORE INTO versions(url, sha256, seen_at) VALUES (?,?,?)", (row["url"], sha, now))

    # ------------------------------------------------------- link expansion
    def _rx(self, patterns: list[str]):
        key = "\x00".join(patterns)
        if key not in self._re_cache:
            self._re_cache[key] = [re.compile(p) for p in patterns]
        return self._re_cache[key]

    def _expand(self, src: Source, row, doc: HtmlDoc) -> None:
        """Queue the files, document pages and further listing pages that a page links to."""
        crawl = src.crawl if row["via"] == "crawl" else None
        hints = json.loads(row["hints"] or "{}")
        page_is_doc = row["kind"] == "page"
        inherit = {k: hints[k] for k in ("language_hint", "institution") if hints.get(k)}
        if page_is_doc and hints.get("issue_date"):
            inherit.update(issue_date=hints["issue_date"], issue_date_source="parent_page_date")
        title = C.good_title(hints.get("title")) or doc.title
        images: list[str] = list(doc.images) if page_is_doc else []
        listing_budget = None

        for link, anchor, in_main in doc.links:
            if is_social(link) or src.excluded(link):
                continue
            ext = extension(link)
            if ext in DOC_EXT or src.is_file_url(link) or is_drive_file(link):
                if self._allowed(src, link):
                    self.db.enqueue(
                        link, src.id, "file", via=row["via"] or "", depth=row["depth"] + 1,
                        parent_url=row["url"] if (in_main and page_is_doc) else None,
                        hints={"anchor": C.good_title(anchor), "parent_title": title if (in_main and page_is_doc) else "",
                               "found_on": row["url"], **inherit},
                    )
                continue
            if ext in IMG_EXT:
                if in_main and page_is_doc and src.allows(link) and link not in images:
                    images.append(link)
                continue
            if not crawl or ext in SKIP_EXT or not src.owns(link):
                continue
            if any(p.search(link) for p in self._rx(crawl.get("deny", []))):
                continue
            allow = self._rx(crawl.get("allow", []))
            if allow and not any(p.search(link) for p in allow):
                continue
            if any(p.search(link) for p in self._rx(crawl.get("doc_page_patterns", []))):
                self.db.enqueue(link, src.id, "page", via="crawl", depth=row["depth"] + 1, hints={"anchor": C.good_title(anchor)})
            elif row["kind"] == "listing":
                paging = any(p.search(link) for p in self._rx(crawl.get("pagination", [r"[?&/]page[=/]\d+", r"[?&](start|offset|p)=\d+"])))
                depth = row["depth"] if paging else row["depth"] + 1
                if depth > int(crawl.get("max_depth", 4)):
                    continue
                if listing_budget is None:
                    listing_budget = int(crawl.get("max_pages", 3000)) - self.db.count("source_id=? AND kind='listing'", (src.id,))
                if listing_budget > 0:
                    listing_budget -= self.db.enqueue(link, src.id, "listing", via="crawl", depth=depth)

        # Scanned notices are often posted as images with one line of text: keep them for OCR.
        want_images = self.s.download_page_images if src.page_images is None else src.page_images
        if page_is_doc and want_images and len(doc.text) < self.s.thin_text_chars:
            for img in images[: self.s.max_page_images]:
                if src.allows(img):
                    self.db.enqueue(img, src.id, "asset", via=row["via"] or "", parent_url=row["url"], depth=row["depth"] + 1)

    # --------------------------------------------------------------- recrawl
    def schedule_recrawl(self, source_ids: list[str] | None = None, days: int | None = None) -> dict[str, int]:
        """Queue listing pages again, and documents not checked for `days` (conditional GET)."""
        days = self.s.recrawl_days if days is None else days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ids = [s.id for s in self.selected(source_ids)]
        marks = ",".join("?" for _ in ids)
        listings = self.db.x(
            f"UPDATE urls SET state='pending', next_attempt_at=0 WHERE kind='listing' AND state='done' AND source_id IN ({marks})", ids)
        docs = self.db.x(
            f"UPDATE urls SET state='pending', next_attempt_at=0 WHERE kind IN ('page','file') AND state='done'"
            f" AND COALESCE(via,'')!=? AND (checked_at IS NULL OR checked_at<=?) AND source_id IN ({marks})",
            [INLINE, cutoff, *ids])
        return {"listings": listings, "documents": docs}

    def retry_failed(self, source_ids: list[str] | None = None) -> int:
        ids = [s.id for s in self.selected(source_ids)]
        marks = ",".join("?" for _ in ids)
        return self.db.x(
            f"UPDATE urls SET state='pending', attempts=0, next_attempt_at=0 WHERE source_id IN ({marks})"
            " AND (state='failed' OR (state='pending' AND next_attempt_at>0))", ids)

    # --------------------------------------------------------------- process
    def _extraction(self, sha: str, ftype: str, url: str) -> tuple[Extracted, str | None]:
        """Extract text once per blob; the result is cached next to the text file."""
        base = self.data / "text" / sha[:2]
        meta_path, text_path = base / f"{sha}.json", base / f"{sha}.txt"
        if meta_path.exists():
            m = json.loads(meta_path.read_text(encoding="utf-8"))
            text = text_path.read_text(encoding="utf-8") if text_path.exists() else ""
            ex = Extracted(text=text, page_count=m.get("page_count"), is_scanned=m.get("is_scanned"), error=m.get("error", ""))
        else:
            ex, _ = extract(self._blob_path(sha, ftype), ftype, url, self.s.scanned_chars_per_page)
            if ex.error == "pymupdf_missing":
                return ex, None  # do not cache: installing PyMuPDF must take effect
            base.mkdir(parents=True, exist_ok=True)
            if ex.text:
                text_path.write_text(ex.text, encoding="utf-8")
            meta_path.write_text(json.dumps({"page_count": ex.page_count, "is_scanned": ex.is_scanned, "error": ex.error}), encoding="utf-8")
        return ex, (text_path.relative_to(self.data).as_posix() if ex.text else None)

    def process(self, source_ids: list[str] | None = None) -> dict[str, int]:
        """Build one record per collected document, with provenance, metadata and eligibility."""
        ids = [s.id for s in (self.selected(source_ids) if source_ids else self.sources.values())]
        marks = ",".join("?" for _ in ids)
        rows = self.db.q(
            f"SELECT * FROM urls WHERE state='done' AND kind IN ('page','file') AND sha256 IS NOT NULL AND source_id IN ({marks})", ids)
        stats = {"records": 0, "eligible": 0, "rejected": 0}
        # Headings shared by many pages of a site are boilerplate (site name, menu), not document titles.
        self._title_freq: dict[str, Counter] = {}
        self._anchor_freq: dict[str, Counter] = {}
        for row in rows:
            if row["page_meta"]:
                self._title_freq.setdefault(row["source_id"], Counter()).update(set(json.loads(row["page_meta"]).get("titles") or []))
            anchor = C.good_title(json.loads(row["hints"] or "{}").get("anchor"))
            if anchor and row["kind"] == "file":
                self._anchor_freq.setdefault(row["source_id"], Counter())[anchor] += 1
        for row in rows:
            try:
                rec = self._build_record(self.sources[row["source_id"]], row)
            except Exception:
                log.exception("could not build a record for %s", row["url"])
                continue
            self.db.upsert_record(rec)
            stats["records"] += 1
            stats[rec["eligibility"]] += 1
        # Records whose URL has since disappeared stay in the corpus (we hold the original) but are flagged.
        self.db.x(
            "UPDATE records SET extra=json_set(extra, '$.source_gone', json('true'))"
            " WHERE url IN (SELECT url FROM urls WHERE state='gone')")
        return stats

    def _build_record(self, src: Source, row) -> dict[str, Any]:
        url, sha, ftype = row["url"], row["sha256"], row["file_type"]
        hints = json.loads(row["hints"] or "{}")
        if hints.get("anchor") and getattr(self, "_anchor_freq", {}).get(src.id, Counter())[C.good_title(hints["anchor"])] > 2:
            hints["anchor"] = ""  # the same label on many files is a section heading, not a title
        meta = json.loads(row["page_meta"] or "{}")
        ex, text_uri = self._extraction(sha, ftype, url)
        text = ex.text
        children = self.db.q("SELECT url, kind FROM urls WHERE parent_url=? AND state='done' AND kind IN ('file','asset')", (url,))
        attachments = [c["url"] for c in children if c["kind"] == "file"]
        assets = [c["url"] for c in children if c["kind"] == "asset"]

        parent_is_landing = False
        if row["parent_url"]:
            p = self.db.one("SELECT page_meta FROM urls WHERE url=?", (row["parent_url"],))
            if p and p["page_meta"]:
                parent_is_landing = json.loads(p["page_meta"]).get("text_chars", 0) < self.s.landing_text_chars

        title = (
            C.good_title(hints.get("title"))
            or (self._page_title(src.id, meta) if ftype == "html" else "")
            or C.good_title(hints.get("anchor"))
            or C.good_title(hints.get("parent_title"))
            or C.good_title((meta.get("filename") or "").rsplit(".", 1)[0].replace("_", " ").replace("-", " "))
            or C.good_title(filename(url).rsplit(".", 1)[0].replace("_", " ").replace("-", " "))
            or filename(url)
        )
        if ftype == "html" and not C.good_title(hints.get("title")) and not self._page_title(src.id, meta):
            # Every heading is site boilerplate: use the first line of the body that reads like a title.
            lines = [ln.strip() for ln in text.split("\n")[:25] if 15 <= len(ln.strip()) <= 300]
            pick = next((ln for ln in lines if C.classify_doc_type(ln)), None) or (lines[0] if lines else "")
            title = C.good_title(pick) or title
        if parent_is_landing and not C.good_title(hints.get("title")) and C.good_title(hints.get("parent_title")):
            # A short post that only introduces its attachment: the post title names the document.
            anchor = C.good_title(hints.get("anchor"))
            title = anchor if anchor and len(self._siblings(row["parent_url"])) > 1 else C.good_title(hints["parent_title"])

        tkey = src.translation_key(url)
        hint_lang = hints.get("language_hint") or (tkey[1] if tkey else "") or src.default_language
        language = C.detect_language(text) or hint_lang or C.detect_language(title, 4) or meta.get("lang", "")
        if language not in ("km", "en", "mixed", "other"):
            language = ""

        if hints.get("doc_type"):
            fam, sub, type_src = hints["doc_type"], hints.get("doc_subtype") or "other", "catalog"
        else:
            guess = C.classify_doc_type(title)
            if guess:
                fam, sub, type_src = *guess, "title_rule"
            else:
                guess = C.classify_doc_type(filename(url), hints.get("parent_title", ""))
                fam, sub, type_src = (*guess, "context_rule") if guess else (src.default_doc_type, "other", "source_default")

        issue_date, date_src = hints.get("issue_date", ""), hints.get("issue_date_source", "")
        if not issue_date:
            issue_date = C.extract_date(title)
            date_src = "title" if issue_date else ""

        thin = ftype == "html" and len(text) < self.s.thin_text_chars
        is_scanned = ex.is_scanned
        needs_ocr = bool(is_scanned) or (thin and bool(assets))
        institution = hints.get("institution") or src.institution
        status = hints.get("status") or "unknown"
        is_press = src.press or sub == "press_release"

        reason = ""
        if ex.error.startswith(("corrupt", "encrypted")):
            reason = "unreadable_file"
        elif ftype == "html" and attachments and len(text) < self.s.landing_text_chars:
            reason = "landing_page"  # the attachment is the document; it inherits this page's title and date
        elif language == "other":
            reason = "language_out_of_scope"
        elif status == "draft":
            reason = "draft_not_issued"
        elif C.looks_personal(title):
            reason = "personal_data"
        elif thin and not assets:
            reason = "thin_content"
        elif not institution:
            reason = "missing_institution"
        elif is_press and not self.s.include_press:
            reason = "press_excluded"

        extra = {k: hints[k] for k in ("title_en", "title_km", "catalog_name", "license", "relations", "original_source", "found_on", "wp_id") if hints.get(k)}
        if attachments:
            extra["attachments"] = attachments
        if ex.error:
            extra["extract_error"] = ex.error
        return {
            "record_id": record_id(src.id, url),
            "url": url,
            "source_id": src.id,
            "institution": institution,
            "institution_km": "" if hints.get("institution") else src.institution_km,
            "doc_type": fam,
            "doc_subtype": sub,
            "doc_type_source": type_src,
            "doc_number": C.norm(hints.get("doc_number") or "") or C.extract_doc_number(title),
            "title": title,
            "language": language,
            "issue_date": issue_date,
            "issue_date_source": date_src,
            "effective_date": hints.get("effective_date", ""),
            "status": status,
            "collected_at": row["fetched_at"],
            "checksum": sha,
            "file_type": ftype,
            "bytes": row["bytes"],
            "reuse_status": hints.get("reuse_status") or src.reuse_status,
            "storage_uri": self._blob_path(sha, ftype).relative_to(self.data).as_posix(),
            "text_uri": text_uri,
            "parent_url": row["parent_url"],
            "landing_url": hints.get("landing_url") or row["parent_url"],
            "group_key": hints.get("group_key") or (tkey[0] if tkey else None),
            "page_count": ex.page_count,
            "text_chars": len(text),
            "is_scanned": None if is_scanned is None else int(is_scanned),
            "needs_ocr": int(needs_ocr),
            "is_press": int(is_press),
            "eligibility": "rejected" if reason else "eligible",
            "reject_reason": reason or None,
            "content_hash": text_hash(text) if len(text) >= 200 else sha,
            "minhash": minhash(text) if len(text) >= 500 else None,
            "assets": json.dumps(assets, ensure_ascii=False),
            "extra": json.dumps(extra, ensure_ascii=False),
            "processed_sha": sha,
            "updated_at": now_iso(),
        }

    def _page_title(self, source_id: str, meta: dict) -> str:
        freq = self._title_freq.get(source_id, Counter())
        for cand in meta.get("titles") or [meta.get("title", "")]:
            if freq[cand] <= 2 and C.good_title(cand):
                return C.good_title(cand)
        return ""

    def _siblings(self, parent_url: str) -> list[str]:
        return [r["url"] for r in self.db.q("SELECT url FROM urls WHERE parent_url=? AND kind='file'", (parent_url,))]


def _disposition_filename(value: str) -> str:
    """File name from a Content-Disposition header (RFC 6266, including filename*=UTF-8'')."""
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", value)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', value)
    return m.group(1).strip() if m else ""
