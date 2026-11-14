"""SQLite crawl state: frontier, blobs, records, cursors, fetch log. This is the checkpoint."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
  url TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  kind TEXT NOT NULL,                 -- listing | page | file | asset
  state TEXT NOT NULL DEFAULT 'pending', -- pending | done | failed | gone | skipped | robots_blocked | offlist
  depth INTEGER NOT NULL DEFAULT 0,
  parent_url TEXT,
  via TEXT,                           -- adapter that discovered it
  hints TEXT NOT NULL DEFAULT '{}',   -- metadata known at discovery time (JSON)
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  last_error TEXT,
  http_status INTEGER,
  etag TEXT,
  last_modified TEXT,
  final_url TEXT,
  sha256 TEXT,
  file_type TEXT,
  bytes INTEGER,
  page_meta TEXT,                     -- title, text length, attachments (JSON)
  discovered_at TEXT,
  fetched_at TEXT,                    -- last time the body was downloaded
  checked_at TEXT,                    -- last time the URL was requested (incl. 304)
  changed_at TEXT                     -- last time the body changed
);
CREATE INDEX IF NOT EXISTS urls_queue ON urls(source_id, state, next_attempt_at);
CREATE INDEX IF NOT EXISTS urls_sha ON urls(sha256);
CREATE INDEX IF NOT EXISTS urls_parent ON urls(parent_url);

CREATE TABLE IF NOT EXISTS blobs (
  sha256 TEXT PRIMARY KEY, bytes INTEGER, file_type TEXT, storage_uri TEXT, first_seen TEXT
);

CREATE TABLE IF NOT EXISTS versions (
  url TEXT NOT NULL, sha256 TEXT NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY (url, sha256)
);

CREATE TABLE IF NOT EXISTS records (
  record_id TEXT PRIMARY KEY,
  url TEXT NOT NULL UNIQUE,
  source_id TEXT NOT NULL,
  institution TEXT, institution_km TEXT,
  doc_type TEXT, doc_subtype TEXT, doc_type_source TEXT,
  doc_number TEXT, title TEXT, language TEXT,
  issue_date TEXT, issue_date_source TEXT, effective_date TEXT,
  status TEXT,
  collected_at TEXT, checksum TEXT, file_type TEXT, bytes INTEGER,
  reuse_status TEXT, storage_uri TEXT, text_uri TEXT,
  parent_url TEXT, landing_url TEXT, group_key TEXT,
  page_count INTEGER, text_chars INTEGER, is_scanned INTEGER, needs_ocr INTEGER, is_press INTEGER,
  eligibility TEXT, reject_reason TEXT,
  dup_of TEXT, dup_reason TEXT, alternate_urls TEXT NOT NULL DEFAULT '[]',
  content_hash TEXT, minhash BLOB,
  assets TEXT NOT NULL DEFAULT '[]', extra TEXT NOT NULL DEFAULT '{}',
  processed_sha TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS records_source ON records(source_id);
CREATE INDEX IF NOT EXISTS records_checksum ON records(checksum);

CREATE TABLE IF NOT EXISTS cursors (
  source_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (source_id, key)
);

CREATE TABLE IF NOT EXISTS fetch_log (
  ts TEXT, source_id TEXT, url TEXT, status INTEGER, bytes INTEGER, elapsed_ms INTEGER, attempts INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS fetch_log_source ON fetch_log(source_id);

CREATE TABLE IF NOT EXISTS host_status (
  host TEXT PRIMARY KEY, source_id TEXT, robots TEXT, blocked TEXT, updated_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DB:
    """One shared connection guarded by a lock; every call commits, so a crash loses nothing."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(args)).fetchone()

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._lock:
            return self.conn.execute(sql, tuple(args)).rowcount

    def many(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    # -- frontier ---------------------------------------------------------
    def enqueue(
        self,
        url: str,
        source_id: str,
        kind: str,
        *,
        via: str = "",
        parent_url: str | None = None,
        depth: int = 0,
        hints: dict[str, Any] | None = None,
    ) -> bool:
        """Add a URL to the frontier. Returns True when it is new.

        An existing URL keeps its state, gains any hints it lacked, and is queued again
        when the discovery source reports a modification newer than our last download.
        """
        hints = {k: v for k, v in (hints or {}).items() if v not in (None, "", [], {})}
        with self._lock:
            row = self.conn.execute(
                "SELECT kind, state, hints, fetched_at, parent_url FROM urls WHERE url=?", (url,)
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO urls(url, source_id, kind, via, parent_url, depth, hints, discovered_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (url, source_id, kind, via, parent_url, depth, json.dumps(hints, ensure_ascii=False), now_iso()),
                )
                return True
            old = json.loads(row["hints"] or "{}")
            merged = {**hints, **old}
            if hints.get("modified"):
                merged["modified"] = max(str(hints["modified"]), str(old.get("modified", "")))
            sets, args = ["hints=?"], [json.dumps(merged, ensure_ascii=False)]
            if row["kind"] == "listing" and kind == "page":
                sets.append("kind='page'")
                if row["state"] == "done":  # listing bodies are not stored; fetch it as a document
                    sets.append("state='pending'")
            if parent_url and not row["parent_url"]:
                sets.append("parent_url=?")
                args.append(parent_url)
            if (
                row["state"] == "done"
                and hints.get("modified")
                and row["fetched_at"]
                and str(hints["modified"])[:19] > row["fetched_at"][:19]
            ):
                sets.append("state='pending'")
            self.conn.execute(f"UPDATE urls SET {', '.join(sets)} WHERE url=?", (*args, url))
            return False

    def next_pending(self, source_id: str, n: int = 20) -> list[sqlite3.Row]:
        # Attachments and images first, then document pages, then listings: a limited (pilot)
        # run reaches documents quickly and completes each page before starting the next.
        return self.q(
            "SELECT * FROM urls WHERE source_id=? AND state='pending' AND next_attempt_at<=?"
            " ORDER BY (kind='listing'), (kind='page'), rowid LIMIT ?",
            (source_id, time.time(), n),
        )

    def update_url(self, url: str, **cols: Any) -> None:
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        self.x(f"UPDATE urls SET {sets} WHERE url=?", (*cols.values(), url))

    def count(self, where: str = "1", args: Iterable[Any] = (), table: str = "urls") -> int:
        return self.one(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", args)["n"]

    # -- cursors ----------------------------------------------------------
    def get_cursor(self, source_id: str, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM cursors WHERE source_id=? AND key=?", (source_id, key))
        return json.loads(row["value"]) if row else default

    def set_cursor(self, source_id: str, key: str, value: Any) -> None:
        self.x(
            "INSERT INTO cursors(source_id, key, value) VALUES (?,?,?)"
            " ON CONFLICT(source_id, key) DO UPDATE SET value=excluded.value",
            (source_id, key, json.dumps(value)),
        )

    # -- records ----------------------------------------------------------
    def upsert_record(self, rec: dict[str, Any]) -> None:
        cols = list(rec)
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "record_id")
        self.x(
            f"INSERT INTO records({','.join(cols)}) VALUES ({placeholders})"
            f" ON CONFLICT(record_id) DO UPDATE SET {updates}",
            [rec[c] for c in cols],
        )
