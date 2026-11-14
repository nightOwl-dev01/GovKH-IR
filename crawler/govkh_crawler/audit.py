"""Source feasibility audit (proposal 6.3): access method, robots.txt, reuse terms, estimated counts."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .db import now_iso
from .discovery import ADAPTERS
from .http import HostBlocked
from .pipeline import Pipeline

FIELDS = ["site_id", "institution", "enabled", "site", "access_method", "listing_pages", "homepage", "robots",
          "content_signals", "estimated_documents", "estimate_basis", "reuse_status", "verdict", "notes"]


def audit_source(pipe: Pipeline, src) -> dict[str, Any]:
    client, ua = pipe.client, pipe.s.user_agent
    origin = src.domains[0] if "//" in src.domains[0] else f"https://{src.domains[0]}"
    row: dict[str, Any] = {
        "site_id": src.id, "institution": src.institution, "enabled": src.enabled, "site": origin,
        "access_method": " + ".join(a.get("type", "?") for a in src.adapters) or "none configured",
        "listing_pages": "; ".join(
            u for a in src.adapters for u in (a.get("start_urls") or a.get("urls") or [x["url"] for x in a.get("apis", [])] or ([a["api"]] if a.get("api") else []))
        )[:400],
        "reuse_status": src.reuse_status, "notes": src.notes.strip(),
    }
    try:
        rb = client.robots(origin + "/", src.verify_tls)
        row["robots"] = rb.summary(ua)
        row["content_signals"] = ",".join(f"{k}={v}" for k, v in rb.signals(ua).items())
        res = client.fetch(origin + "/", delay=src.delay, verify=src.verify_tls, retries=1)
        res.discard()
        row["homepage"] = str(res.status) if res.status else res.error
        total, basis, known = 0, [], False
        for cfg in src.adapters:
            if cfg.get("type") not in ADAPTERS:
                continue
            n, note = ADAPTERS[cfg["type"]](pipe.s, pipe.db, client, src, cfg).estimate()
            if n is not None:
                total, known = total + n, True
            basis.append(note)
        row["estimated_documents"] = total if known else ""
        row["estimate_basis"] = "; ".join(basis)
    except HostBlocked as e:
        row.setdefault("robots", "unknown")
        row["homepage"] = row.get("homepage") or f"paused: {e}"

    home_ok = row.get("homepage") == "200"
    if row.get("robots") == "unreachable":
        verdict = "no-go: host unreachable (robots.txt could not be read)"
    elif "fully disallowed" in row.get("robots", ""):
        verdict = "no-go: robots.txt does not permit crawling"
    elif not home_ok:
        verdict = f"no-go: site not reachable by a polite crawler ({row.get('homepage')})"
    elif not src.adapters:
        verdict = "needs an adapter"
    elif not src.enabled:
        verdict = "disabled after the pilot: see notes"
    elif row.get("estimated_documents") == 0:
        verdict = "check: adapter found no documents"
    else:
        verdict = "go: run the pilot"
    row["verdict"] = verdict
    return row


def run_audit(pipe: Pipeline, source_ids: list[str] | None = None) -> list[dict[str, Any]]:
    sources = [pipe.sources[i] for i in source_ids] if source_ids else list(pipe.sources.values())
    results: list[dict[str, Any]] = []

    def one(src) -> None:
        results.append(audit_source(pipe, src))

    pipe._parallel(one, sources)
    results.sort(key=lambda r: list(pipe.sources).index(r["site_id"]))
    write_audit(results, pipe.data / "reports")
    return results


def write_audit(rows: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "source_audit.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    known = sum(int(r["estimated_documents"]) for r in rows if r.get("estimated_documents") not in ("", None) and r["verdict"].startswith("go"))
    lines = [
        "# GovKH Search: source feasibility audit",
        f"Generated {now_iso()}. Estimates are upper bounds before eligibility checks and deduplication.",
        f"Estimated documents across sources with a `go` verdict: **{known:,}**.",
        "",
        "| Source | Institution | Access | Homepage | robots.txt | Content signals | Est. documents | Reuse | Verdict |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(k, "") if r.get(k) is not None else "") for k in (
            "site_id", "institution", "access_method", "homepage", "robots", "content_signals", "estimated_documents", "reuse_status", "verdict")) + " |")
    lines += ["", "## Notes per source", ""]
    for r in rows:
        lines.append(f"- **{r['site_id']}** ({r['site']}): {r.get('estimate_basis') or 'no estimate'}." + (f" {r['notes']}" if r.get("notes") else ""))
    (out_dir / "source_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
