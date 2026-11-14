"""Command line: python -m govkh_crawler <command>."""
from __future__ import annotations

import logging
import signal
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from . import manifest
from .audit import run_audit
from .config import DEFAULT_CONFIG_DIR, ROOT, load_settings, load_sources
from .dedup import run_dedup
from .pipeline import Pipeline

app = typer.Typer(add_completion=False, no_args_is_help=True, help="GovKH Search crawling pipeline.")
console = Console()
state: dict = {}

SourceOpt = typer.Option(None, "--source", "-s", help="Source id (repeatable). Default: every enabled source.")
LimitOpt = typer.Option(None, "--limit", "-n", help="Stop after this many documents per source.")


@app.callback()
def main(
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, help="Folder with settings.yaml and sources.yaml."),
    data_dir: Optional[Path] = typer.Option(None, help="Override the data folder from settings.yaml."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    settings = load_settings(config_dir / "settings.yaml")
    if data_dir:
        settings.data_dir = str(data_dir.resolve())
    state.update(settings=settings, sources=load_sources(config_dir / "sources.yaml"), verbose=verbose)


def _pipeline() -> Pipeline:
    pipe = Pipeline(state["settings"], state["sources"], ROOT)
    log_dir = pipe.data / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / f"crawl-{datetime.now():%Y%m%d}.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(
        level=logging.DEBUG if state["verbose"] else logging.INFO,
        format="%(message)s", datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, show_path=False, markup=False), file_handler], force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    def on_signal(signum, frame):  # first Ctrl-C: stop cleanly; second: default behaviour
        console.print("[yellow]Stopping after the requests in flight. Progress is saved; rerun to resume.[/]")
        pipe.stop.set()
        signal.signal(signal.SIGINT, signal.default_int_handler)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    return pipe


def _finish(pipe: Pipeline) -> dict:
    """process -> dedup -> export, the offline half of the pipeline."""
    stats = pipe.process()
    dd = run_dedup(pipe.db, {s.id: s.priority for s in pipe.sources.values()}, pipe.s.near_dup_threshold)
    report = manifest.export(pipe.db, pipe.s, pipe.sources, pipe.data)
    f, acc = report["funnel"], report["acceptance"]
    console.print(
        f"records {stats['records']} | rejected {f['rejected']} | duplicates {dd['duplicates']} | "
        f"[bold]retained {f['retained']}[/] of target {acc['target']} "
        f"({'[green]met[/]' if acc['meets_target'] else '[yellow]not yet met[/]'})"
    )
    console.print(f"manifest: {pipe.data / 'manifest' / 'manifest.jsonl'}\nreport:   {pipe.data / 'reports' / 'corpus_report.md'}")
    return report


@app.command()
def sources() -> None:
    """List the source registry."""
    t = Table("id", "enabled", "institution", "domains", "adapters")
    for s in state["sources"].values():
        t.add_row(s.id, "yes" if s.enabled else "no", s.institution, ", ".join(s.domains), ", ".join(a.get("type", "?") for a in s.adapters))
    console.print(t)


@app.command()
def audit(source: Optional[list[str]] = SourceOpt) -> None:
    """Week 1 source audit: robots.txt, reachability and estimated counts for every registered source."""
    pipe = _pipeline()
    try:
        rows = run_audit(pipe, source)
    finally:
        pipe.close()
    t = Table("source", "homepage", "robots", "est. docs", "verdict")
    for r in rows:
        t.add_row(r["site_id"], str(r.get("homepage", "")), str(r.get("robots", ""))[:48], str(r.get("estimated_documents", "")), r["verdict"])
    console.print(t)
    console.print(f"written: {pipe.data / 'reports' / 'source_audit.md'}")


@app.command()
def discover(source: Optional[list[str]] = SourceOpt, limit: Optional[int] = LimitOpt) -> None:
    """Find document URLs (sitemaps, APIs, listing pages) and add them to the frontier."""
    pipe = _pipeline()
    try:
        pipe.discover(source, limit)
    finally:
        pipe.close()


@app.command()
def fetch(source: Optional[list[str]] = SourceOpt, limit: Optional[int] = LimitOpt) -> None:
    """Download pending URLs politely. Safe to interrupt and rerun."""
    pipe = _pipeline()
    try:
        pipe.fetch(source, limit)
    finally:
        pipe.close()


@app.command()
def process() -> None:
    """Build records from downloaded files, deduplicate, and export the manifest and report."""
    pipe = _pipeline()
    try:
        _finish(pipe)
    finally:
        pipe.close()


@app.command()
def run(source: Optional[list[str]] = SourceOpt, limit: Optional[int] = LimitOpt) -> None:
    """Full pipeline: discover, fetch, process, deduplicate, export."""
    pipe = _pipeline()
    try:
        pipe.run(source, limit)
        _finish(pipe)
    finally:
        pipe.close()


@app.command()
def pilot(source: Optional[list[str]] = SourceOpt, limit: int = typer.Option(200, "--limit", "-n", help="Documents per source (proposal: 100 to 300).")) -> None:
    """Week 1 pilot: a bounded run per source; the report gives success, scan, duplicate and language rates."""
    run(source, limit)


@app.command()
def recrawl(
    source: Optional[list[str]] = SourceOpt,
    days: Optional[int] = typer.Option(None, help="Re-check documents not checked for this many days (default: recrawl_days)."),
) -> None:
    """Weekly incremental crawl: new documents plus conditional re-checks; writes manifest/changes.jsonl."""
    pipe = _pipeline()
    try:
        queued = pipe.schedule_recrawl(source, days)
        console.print(f"re-queued {queued['listings']} listing pages and {queued['documents']} documents for conditional re-check")
        pipe.discover(source)
        if not pipe.stop.is_set():
            pipe.fetch(source)
        report = _finish(pipe)
        console.print(f"changes since last export: {report['changes_since_last_export'] or 'none'}")
    finally:
        pipe.close()


@app.command("retry-failed")
def retry_failed(source: Optional[list[str]] = SourceOpt) -> None:
    """Put failed and backed-off URLs back in the queue (then run `fetch`)."""
    pipe = _pipeline()
    try:
        console.print(f"re-queued {pipe.retry_failed(source)} URLs")
    finally:
        pipe.close()


@app.command()
def status() -> None:
    """Frontier and record counts per source."""
    pipe = _pipeline()
    try:
        rows = pipe.db.q("SELECT source_id, state, COUNT(*) AS n FROM urls WHERE kind IN ('page','file') GROUP BY source_id, state")
        recs = {r["source_id"]: r["n"] for r in pipe.db.q(
            "SELECT source_id, COUNT(*) AS n FROM records WHERE eligibility='eligible' AND dup_of IS NULL GROUP BY source_id")}
    finally:
        pipe.close()
    states = ["pending", "done", "failed", "gone", "skipped", "robots_blocked", "offlist"]
    counts: dict[str, dict[str, int]] = {}
    for r in rows:
        counts.setdefault(r["source_id"], {})[r["state"]] = r["n"]
    t = Table("source", *states, "retained")
    for sid in sorted(counts):
        t.add_row(sid, *(str(counts[sid].get(s, 0)) for s in states), str(recs.get(sid, 0)))
    t.add_row("TOTAL", *(str(sum(c.get(s, 0) for c in counts.values())) for s in states), str(sum(recs.values())), style="bold")
    console.print(t)


if __name__ == "__main__":
    app()
