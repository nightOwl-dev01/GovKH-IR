#!/usr/bin/env bash
# Full collection: discover + fetch every enabled source, retry failures once, then build the manifest.
# Safe to stop (kill -INT <pid>) and start again; it resumes from data/state.sqlite.
set -u
cd "$(dirname "$0")"
PY=../.venv/bin/python
export COLUMNS=200
echo "== start $(date -Is)"
$PY -m govkh_crawler run
echo "== retry failed $(date -Is)"
$PY -m govkh_crawler retry-failed
$PY -m govkh_crawler fetch
$PY -m govkh_crawler process
echo "== done $(date -Is)"
