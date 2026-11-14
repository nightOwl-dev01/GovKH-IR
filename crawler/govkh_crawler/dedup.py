"""Deduplication (proposal 6.4): exact checksum, normalised-text hash, and MinHash near-duplicates."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict

import numpy as np

from .classify import norm
from .db import DB, now_iso

N_PERM = 64
BANDS, ROWS = 16, 4
SHINGLE = 5
MAX_CHARS = 200_000
_rng = np.random.default_rng(20261114)
_A = (_rng.integers(1, 2**63, size=N_PERM, dtype=np.uint64) << np.uint64(1)) | np.uint64(1)
_B = _rng.integers(0, 2**63, size=N_PERM, dtype=np.uint64)
_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)


def normalise_for_hash(text: str) -> str:
    """Text reduced to its letters and digits: layout, spacing and punctuation do not matter."""
    return _PUNCT.sub("", norm(text).lower())


def text_hash(text: str) -> str:
    return hashlib.sha256(normalise_for_hash(text).encode("utf-8")).hexdigest()


def minhash(text: str) -> bytes | None:
    """64-permutation MinHash over character 5-grams (works for unsegmented Khmer)."""
    t = normalise_for_hash(text)[:MAX_CHARS]
    if len(t) < SHINGLE + 20:
        return None
    cp = np.frombuffer(t.encode("utf-32-le"), dtype=np.uint32).astype(np.uint64)
    n = len(cp) - SHINGLE + 1
    h = np.zeros(n, dtype=np.uint64)
    with np.errstate(over="ignore"):
        for j in range(SHINGLE):
            h = h * np.uint64(1099511628211) + cp[j : j + n]
        h = np.unique(h)
        h ^= h >> np.uint64(33)
        h *= np.uint64(0xFF51AFD7ED558CCD)
        h ^= h >> np.uint64(33)
        sig = np.empty(N_PERM, dtype=np.uint64)
        for i in range(N_PERM):
            sig[i] = (h * _A[i] + _B[i]).min()
    return sig.tobytes()


def similarity(a: bytes, b: bytes) -> float:
    return float((np.frombuffer(a, dtype=np.uint64) == np.frombuffer(b, dtype=np.uint64)).mean())


class _UF:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _conflict(a, b) -> bool:
    """Different instruments that merely share a template must stay separate records."""
    for key in ("doc_number", "issue_date"):
        if a[key] and b[key] and a[key] != b[key]:
            return True
    return bool(a["language"] and b["language"] and a["language"] != b["language"])


def run_dedup(db: DB, priorities: dict[str, int], threshold: float = 0.9) -> dict[str, int]:
    """Mark duplicates among eligible records. Idempotent: recomputed from scratch each time."""
    db.x("UPDATE records SET dup_of=NULL, dup_reason=NULL, alternate_urls='[]'")
    rows = db.q(
        "SELECT record_id, url, source_id, checksum, content_hash, minhash, text_chars, doc_number,"
        " issue_date, language, collected_at, file_type FROM records WHERE eligibility='eligible'"
    )
    by_id = {r["record_id"]: r for r in rows}
    uf = _UF()
    reason: dict[str, str] = {}
    stats = {"exact_checksum": 0, "normalized_text": 0, "near_duplicate": 0}

    def link(a: str, b: str, why: str) -> None:
        if uf.find(a) != uf.find(b):
            uf.union(a, b)
            reason.setdefault(b, why)
            reason.setdefault(a, why)

    # 1. identical bytes
    groups: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r["checksum"]:
            groups[r["checksum"]].append(r["record_id"])
    for ids in groups.values():
        for other in ids[1:]:
            link(ids[0], other, "exact_checksum")

    # 2. identical text after normalisation (same instrument re-encoded or re-hosted)
    groups = defaultdict(list)
    for r in rows:
        if r["content_hash"] and (r["text_chars"] or 0) >= 200:
            groups[r["content_hash"]].append(r["record_id"])
    for ids in groups.values():
        for other in ids[1:]:
            if not _conflict(by_id[ids[0]], by_id[other]):
                link(ids[0], other, "normalized_text")

    # 3. near duplicates: LSH banding, then verify on the full signature
    buckets: dict[tuple[int, bytes], list[str]] = defaultdict(list)
    for r in rows:
        if r["minhash"] and (r["text_chars"] or 0) >= 500:
            sig = r["minhash"]
            for b in range(BANDS):
                buckets[(b, sig[b * ROWS * 8 : (b + 1) * ROWS * 8])].append(r["record_id"])
    checked: set[tuple[str, str]] = set()
    for ids in buckets.values():
        if len(ids) < 2:
            continue
        anchors = ids[:5]  # bounded comparisons, even for very large template buckets
        for a in anchors:
            for b in ids:
                if a == b or (a, b) in checked or (b, a) in checked:
                    continue
                checked.add((a, b))
                ra, rb = by_id[a], by_id[b]
                ta, tb = ra["text_chars"] or 1, rb["text_chars"] or 1
                if min(ta, tb) / max(ta, tb) < 0.8 or _conflict(ra, rb):
                    continue
                if similarity(ra["minhash"], rb["minhash"]) >= threshold:
                    link(a, b, "near_duplicate")

    # Choose the record to keep per cluster: issuer site over archive, then the earliest copy.
    clusters: dict[str, list[str]] = defaultdict(list)
    for rid in list(uf.p):
        clusters[uf.find(rid)].append(rid)
    updates = []
    for ids in clusters.values():
        if len(ids) < 2:
            continue
        ids.sort(key=lambda i: (-priorities.get(by_id[i]["source_id"], 0), by_id[i]["collected_at"] or "", len(by_id[i]["url"]), i))
        keep = ids[0]
        alternates = [by_id[i]["url"] for i in ids[1:]]
        db.x("UPDATE records SET alternate_urls=? WHERE record_id=?", (json.dumps(alternates, ensure_ascii=False), keep))
        for i in ids[1:]:
            why = reason.get(i, "near_duplicate")
            stats[why] += 1
            updates.append((keep, why, now_iso(), i))
    db.many("UPDATE records SET dup_of=?, dup_reason=?, updated_at=? WHERE record_id=?", updates)
    stats["duplicates"] = len(updates)
    stats["clusters"] = sum(1 for ids in clusters.values() if len(ids) > 1)
    return stats
