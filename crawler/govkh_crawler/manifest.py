"""Corpus manifest, relations, change list and the corpus audit report (proposal 6.4, 6.5)."""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import Settings, Source
from .db import DB, now_iso

REQUIRED = ("source_id", "canonical_url", "institution", "doc_type", "language", "collected_at", "checksum")
# Column order of the CSV manifest. The first block mirrors the `sources` table of the proposal (section 11).
COLUMNS = [
    "source_id", "canonical_url", "alternate_urls", "institution", "doc_type", "doc_number", "title", "language",
    "issue_date", "effective_date", "status", "collected_at", "checksum", "file_type", "reuse_status", "storage_uri",
    "site_id", "institution_km", "doc_subtype", "doc_type_source", "issue_date_source", "bytes", "page_count",
    "text_chars", "text_uri", "is_scanned", "needs_ocr", "is_press", "landing_url", "group_key", "content_hash",
    "assets", "extra",
]


def _row_to_manifest(r) -> dict[str, Any]:
    return {
        "source_id": r["record_id"],
        "canonical_url": r["url"],
        "alternate_urls": json.loads(r["alternate_urls"] or "[]"),
        "institution": r["institution"],
        "doc_type": r["doc_type"],
        "doc_number": r["doc_number"] or "",
        "title": r["title"],
        "language": r["language"],
        "issue_date": r["issue_date"] or "",
        "effective_date": r["effective_date"] or "",
        "status": r["status"],
        "collected_at": r["collected_at"],
        "checksum": r["checksum"],
        "file_type": r["file_type"],
        "reuse_status": r["reuse_status"],
        "storage_uri": r["storage_uri"],
        "site_id": r["source_id"],
        "institution_km": r["institution_km"] or "",
        "doc_subtype": r["doc_subtype"],
        "doc_type_source": r["doc_type_source"],
        "issue_date_source": r["issue_date_source"] or "",
        "bytes": r["bytes"],
        "page_count": r["page_count"],
        "text_chars": r["text_chars"],
        "text_uri": r["text_uri"] or "",
        "is_scanned": None if r["is_scanned"] is None else bool(r["is_scanned"]),
        "needs_ocr": bool(r["needs_ocr"]),
        "is_press": bool(r["is_press"]),
        "landing_url": r["landing_url"] or "",
        "group_key": r["group_key"] or "",
        "content_hash": r["content_hash"],
        "assets": json.loads(r["assets"] or "[]"),
        "extra": json.loads(r["extra"] or "{}"),
    }


def _write_jsonl(path: Path, items: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _relations(retained: list[dict]) -> list[dict]:
    out: list[dict] = []
    groups: dict[str, list[dict]] = defaultdict(list)
    by_catalog: dict[str, list[dict]] = defaultdict(list)
    for m in retained:
        if m["group_key"]:
            groups[m["group_key"]].append(m)
        if m["extra"].get("catalog_name"):
            by_catalog[m["extra"]["catalog_name"]].append(m)
    for key, members in groups.items():
        km = [m for m in members if m["language"] == "km"]
        en = [m for m in members if m["language"] == "en"]
        for e in en:
            for k in km:
                out.append({"from_source_id": e["source_id"], "to_source_id": k["source_id"], "relation": "translation_of", "evidence": key})
    for m in retained:
        for relation, names in (m["extra"].get("relations") or {}).items():
            for name in names:
                for target in by_catalog.get(name, []):
                    if target["source_id"] != m["source_id"] and (target["language"] == m["language"] or len(by_catalog[name]) == 1):
                        out.append({"from_source_id": m["source_id"], "to_source_id": target["source_id"], "relation": relation, "evidence": f"catalog:{name}"})
    return out


def export(db: DB, settings: Settings, sources: dict[str, Source], data_dir: Path) -> dict[str, Any]:
    out_dir, rep_dir = data_dir / "manifest", data_dir / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)
    rows = db.q("SELECT * FROM records ORDER BY source_id, record_id")
    retained = [_row_to_manifest(r) for r in rows if r["eligibility"] == "eligible" and not r["dup_of"]]
    excluded = [
        {"source_id": r["record_id"], "canonical_url": r["url"], "site_id": r["source_id"], "title": r["title"],
         "reason": r["reject_reason"] or f"duplicate:{r['dup_reason']}", "duplicate_of": r["dup_of"] or "",
         "checksum": r["checksum"], "storage_uri": r["storage_uri"]}
        for r in rows if r["eligibility"] != "eligible" or r["dup_of"]
    ]

    manifest_path = out_dir / "manifest.jsonl"
    previous: dict[str, str] = {}
    if manifest_path.exists():
        with manifest_path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    item = json.loads(line)
                    previous[item["source_id"]] = item.get("content_hash", "")
    current = {m["source_id"]: m["content_hash"] for m in retained}
    changes = (
        [{"source_id": i, "change": "added"} for i in current if i not in previous]
        + [{"source_id": i, "change": "changed"} for i in current if i in previous and previous[i] != current[i]]
        + [{"source_id": i, "change": "removed"} for i in previous if i not in current]
    )

    _write_jsonl(manifest_path, retained)
    _write_jsonl(out_dir / "excluded.jsonl", excluded)
    _write_jsonl(out_dir / "relations.jsonl", _relations(retained))
    _write_jsonl(out_dir / "changes.jsonl", changes)  # what the indexer must re-index after this export
    with (out_dir / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for m in retained:
            w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else ("" if v is None else v) for k, v in m.items()})

    report = build_report(db, settings, sources, rows, retained, changes)
    (rep_dir / "corpus_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (rep_dir / "corpus_report.md").write_text(render_report(report), encoding="utf-8")
    return report


def build_report(db: DB, settings: Settings, sources: dict[str, Source], rows, retained: list[dict], changes: list[dict]) -> dict[str, Any]:
    states = {(r["source_id"], r["kind"], r["state"]): r["n"] for r in db.q(
        "SELECT source_id, kind, state, COUNT(*) AS n FROM urls GROUP BY source_id, kind, state")}

    def urls(source: str | None, kinds: tuple[str, ...], state: str | None = None) -> int:
        return sum(n for (s, k, st), n in states.items() if (source is None or s == source) and k in kinds and (state is None or st == state))

    docs = ("page", "file")
    eligible = [r for r in rows if r["eligibility"] == "eligible"]
    dups = [r for r in eligible if r["dup_of"]]
    rejected = [r for r in rows if r["eligibility"] != "eligible"]
    funnel = {
        "discovered": urls(None, docs),
        "downloaded": urls(None, docs, "done"),
        "pending": urls(None, docs, "pending"),
        "failed": urls(None, docs, "failed"),
        "gone_404": urls(None, docs, "gone"),
        "skipped": urls(None, docs, "skipped"),
        "robots_blocked": urls(None, docs, "robots_blocked"),
        "off_whitelist": urls(None, docs, "offlist"),
        "parsed": sum(1 for r in rows if not json.loads(r["extra"] or "{}").get("extract_error")),
        "rejected": len(rejected),
        "deduplicated": len(dups),
        "retained": len(retained),
        "retained_excluding_press": sum(1 for m in retained if not m["is_press"]),
        "ocr_assets": urls(None, ("asset",), "done"),
    }

    per_source = []
    for sid in sorted({r["source_id"] for r in rows} | {s for s, _, _ in states}):
        mine = [m for m in retained if m["site_id"] == sid]
        pdfs = [m for m in mine if m["file_type"] == "pdf"]
        done, failed, gone = urls(sid, docs, "done"), urls(sid, docs, "failed"), urls(sid, docs, "gone")
        tried = done + failed + gone
        src_dups = sum(1 for r in dups if r["source_id"] == sid)
        src_elig = sum(1 for r in eligible if r["source_id"] == sid)
        per_source.append({
            "site_id": sid,
            "institution": sources[sid].institution if sid in sources else "",
            "discovered": urls(sid, docs),
            "downloaded": done,
            "pending": urls(sid, docs, "pending"),
            "failed": failed + gone,
            "download_success": round(done / tried, 3) if tried else None,
            "rejected": sum(1 for r in rejected if r["source_id"] == sid),
            "duplicates": src_dups,
            "duplicate_rate": round(src_dups / src_elig, 3) if src_elig else None,
            "retained": len(mine),
            "scanned_pdf_share": round(sum(1 for m in pdfs if m["is_scanned"]) / len(pdfs), 3) if pdfs else None,
            "needs_ocr": sum(1 for m in mine if m["needs_ocr"]),
            "km": sum(1 for m in mine if m["language"] == "km"),
            "en": sum(1 for m in mine if m["language"] == "en"),
            "mixed": sum(1 for m in mine if m["language"] == "mixed"),
            "with_doc_number": sum(1 for m in mine if m["doc_number"]),
            "with_issue_date": sum(1 for m in mine if m["issue_date"]),
        })

    n = len(retained) or 1
    completeness = {f: round(sum(1 for m in retained if m.get(f) not in (None, "", [])) / n, 4)
                    for f in (*REQUIRED, "title", "doc_number", "issue_date", "status_known")
                    if f != "status_known"}
    completeness["status_known"] = round(sum(1 for m in retained if m["status"] != "unknown") / n, 4)
    missing_required = sum(1 for m in retained if any(m.get(f) in (None, "") for f in REQUIRED))
    by_type = Counter(m["doc_type"] for m in retained)
    errors = db.q("SELECT source_id, last_error, COUNT(*) AS n FROM urls WHERE state IN ('failed','skipped','offlist','robots_blocked')"
                  " GROUP BY source_id, last_error ORDER BY n DESC LIMIT 25")
    return {
        "generated_at": now_iso(),
        "user_agent": settings.user_agent,
        "funnel": funnel,
        "rejected_by_reason": dict(Counter(r["reject_reason"] for r in rejected).most_common()),
        "duplicates_by_reason": dict(Counter(r["dup_reason"] for r in dups).most_common()),
        "by_doc_type": {t: {"retained": by_type.get(t, 0), "planning_target": settings.planning_targets.get(t)} for t in settings.planning_targets},
        "by_doc_subtype": dict(Counter(m["doc_subtype"] for m in retained).most_common()),
        "by_language": dict(Counter(m["language"] or "undetermined" for m in retained).most_common()),
        "by_file_type": dict(Counter(m["file_type"] for m in retained).most_common()),
        "by_status": dict(Counter(m["status"] for m in retained).most_common()),
        "by_reuse_status": dict(Counter(m["reuse_status"] for m in retained).most_common()),
        "metadata_completeness": completeness,
        "per_source": per_source,
        "acceptance": {
            "target": settings.corpus_target,
            "retained": len(retained),
            "meets_target": len(retained) >= settings.corpus_target,
            "records_missing_required_fields": missing_required,
            "required_fields_complete": missing_required == 0,
            "reuse_status_recorded": all(m["reuse_status"] for m in retained),
        },
        "changes_since_last_export": dict(Counter(c["change"] for c in changes)),
        "paused_hosts": [dict(r) for r in db.q("SELECT host, blocked, updated_at FROM host_status WHERE blocked IS NOT NULL AND blocked!=''")],
        "top_errors": [dict(r) for r in errors],
    }


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v * 100:.1f}%"


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join("–" if c is None else str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def render_report(rep: dict[str, Any]) -> str:
    f, acc = rep["funnel"], rep["acceptance"]
    mark = lambda ok: "PASS" if ok else "FAIL"  # noqa: E731
    parts = [
        "# GovKH Search: corpus audit report",
        f"Generated {rep['generated_at']} by `{rep['user_agent']}`.",
        "## Acceptance criteria (proposal 6.5)",
        _table(["Criterion", "Result", "Detail"], [
            [f"At least {acc['target']:,} unique retained documents", mark(acc["meets_target"]), f"{acc['retained']:,} retained ({f['retained_excluding_press']:,} excluding press releases)"],
            ["Every record has ID, URL, institution, type, language, timestamp, checksum", mark(acc["required_fields_complete"]), f"{acc['records_missing_required_fields']} record(s) missing a required field"],
            ["Reuse status recorded for every record", mark(acc["reuse_status_recorded"]), ", ".join(f"{k}: {v}" for k, v in rep["by_reuse_status"].items()) or "–"],
        ]),
        "## Collection funnel (proposal 6.4)",
        _table(["Stage", "Documents"], [
            ["Discovered (document URLs in the frontier)", f["discovered"]],
            ["Downloaded", f["downloaded"]],
            ["Still pending", f["pending"]],
            ["Failed after retries", f["failed"]],
            ["Gone (404/410)", f["gone_404"]],
            ["Skipped (too large, wrong type, excluded)", f["skipped"]],
            ["Blocked by robots.txt", f["robots_blocked"]],
            ["Off the official-source whitelist", f["off_whitelist"]],
            ["Parsed without extraction error", f["parsed"]],
            ["Rejected by eligibility checks", f["rejected"]],
            ["Removed as duplicates", f["deduplicated"]],
            ["**Retained**", f"**{f['retained']}**"],
            ["Image assets kept for OCR (not counted)", f["ocr_assets"]],
        ]),
        "### Rejected, by reason",
        _table(["Reason", "Records"], [[k, v] for k, v in rep["rejected_by_reason"].items()] or [["none", 0]]),
        "### Duplicates, by method",
        _table(["Method", "Records"], [[k, v] for k, v in rep["duplicates_by_reason"].items()] or [["none", 0]]),
        "## Retained documents by type, against the planning targets (proposal 6.1)",
        _table(["Source family", "Retained", "Planning target"], [[k, v["retained"], v["planning_target"]] for k, v in rep["by_doc_type"].items()]),
        "Subtypes: " + (", ".join(f"{k} {v}" for k, v in rep["by_doc_subtype"].items()) or "–"),
        "## Language, file type, legal status",
        _table(["Dimension", "Split"], [
            ["Language", ", ".join(f"{k} {v}" for k, v in rep["by_language"].items()) or "–"],
            ["File type", ", ".join(f"{k} {v}" for k, v in rep["by_file_type"].items()) or "–"],
            ["Status", ", ".join(f"{k} {v}" for k, v in rep["by_status"].items()) or "–"],
        ]),
        "## Metadata completeness (share of retained records)",
        _table(["Field", "Complete"], [[k, _pct(v)] for k, v in rep["metadata_completeness"].items()]),
        "## Per source",
        _table(
            ["Source", "Discovered", "Downloaded", "Pending", "Failed", "Success", "Rejected", "Dups", "Dup rate", "Retained", "Scanned PDFs", "Needs OCR", "km", "en", "mixed", "Doc no.", "Issue date"],
            [[s["site_id"], s["discovered"], s["downloaded"], s["pending"], s["failed"], _pct(s["download_success"]), s["rejected"], s["duplicates"],
              _pct(s["duplicate_rate"]), s["retained"], _pct(s["scanned_pdf_share"]), s["needs_ocr"], s["km"], s["en"], s["mixed"], s["with_doc_number"], s["with_issue_date"]]
             for s in rep["per_source"]],
        ),
    ]
    if rep["paused_hosts"]:
        parts += ["## Hosts paused after repeated failures", _table(["Host", "Reason", "When"], [[h["host"], h["blocked"], h["updated_at"]] for h in rep["paused_hosts"]])]
    if rep["top_errors"]:
        parts += ["## Most frequent fetch problems", _table(["Source", "Problem", "URLs"], [[e["source_id"], e["last_error"], e["n"]] for e in rep["top_errors"]])]
    ch = rep["changes_since_last_export"]
    parts += ["## Changes since the previous export", ", ".join(f"{k}: {v}" for k, v in ch.items()) or "No changes."]
    return "\n\n".join(parts) + "\n"
