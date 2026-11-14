"""Pipeline CLI: python -m govkh <step> [...]. Run from Project/backend. `all` runs the offline build in order."""
from __future__ import annotations

import argparse
import json
import time


def main() -> None:
    p = argparse.ArgumentParser(prog="govkh")
    p.add_argument("step", choices=["initdb", "load", "parse", "ocr", "tables", "enrich", "chunk", "index", "embed", "all", "stats"])
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--limit", type=int)
    p.add_argument("--llm", action="store_true", help="enrich: let the language model fill what the rules missed")
    p.add_argument("--strategy", default="structure,fixed")
    a = p.parse_args()

    from . import db, ingest

    def run(step: str):
        t = time.time()
        if step == "initdb":
            out = {"pgvector": db.init()}
        elif step == "load":
            out = ingest.load()
        elif step == "parse":
            out = ingest.parse(a.workers)
        elif step == "ocr":
            out = ingest.ocr(a.workers, a.limit)
        elif step == "tables":
            out = ingest.tables(a.limit)
        elif step == "enrich":
            from . import enrich
            out = enrich.run(llm=a.llm, limit=a.limit)
        elif step == "chunk":
            from . import chunk
            out = chunk.run(a.workers)
        elif step == "index":
            from . import lexical
            out = lexical.build(a.strategy.split(","), a.workers)
        elif step == "embed":
            from . import dense
            out = dense.build(a.strategy.split(","))
        elif step == "stats":
            from . import stats
            out = stats.corpus()
        print(f"[{step}] {time.time() - t:.1f}s {json.dumps(out, ensure_ascii=False, default=str)}", flush=True)

    if a.step == "all":
        for s in ["initdb", "load", "parse", "ocr", "parse", "tables", "enrich", "chunk", "index", "embed"]:
            run(s)
    else:
        run(a.step)


if __name__ == "__main__":
    main()
