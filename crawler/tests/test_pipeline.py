"""End-to-end: discover, fetch, process, dedup, export, then an incremental recrawl."""
import json

from govkh_crawler import manifest
from govkh_crawler.dedup import run_dedup
from govkh_crawler.manifest import REQUIRED


def finish(pipe):
    pipe.process()
    run_dedup(pipe.db, {s.id: s.priority for s in pipe.sources.values()}, pipe.s.near_dup_threshold)
    return manifest.export(pipe.db, pipe.s, pipe.sources, pipe.data)


def load(pipe, name):
    return [json.loads(line) for line in (pipe.data / "manifest" / name).read_text(encoding="utf-8").splitlines() if line]


def test_full_pipeline(pipe, site):
    pipe.discover()
    pipe.fetch()
    report = finish(pipe)
    kept = {m["canonical_url"].replace(site.base, ""): m for m in load(pipe, "manifest.jsonl")}
    excluded = {m["canonical_url"].replace(site.base, ""): m for m in load(pipe, "excluded.jsonl")}
    state = {r["url"].replace(site.base, ""): r["state"] for r in pipe.db.q("SELECT url, state FROM urls")}

    # Retained: the two language versions of the law, one copy of the report, the retried PDF, the scan, the WP post.
    assert set(kept) == {"/kh/law/1", "/en/law/1", "/files/report.pdf", "/files/flaky.pdf", "/files/scan.pdf", "/post/1"}

    # Provenance: every retained record carries the required fields, and the original file is on disk.
    for m in kept.values():
        assert all(m[f] for f in REQUIRED), m
        assert m["institution"] == "Ministry of Testing" and m["reuse_status"] == "unclear"
        assert (pipe.data / m["storage_uri"]).exists() and len(m["checksum"]) == 64

    law = kept["/kh/law/1"]
    assert (law["language"], law["doc_type"], law["doc_number"]) == ("km", "legal_instrument", "123 នស/រកម")
    assert kept["/en/law/1"]["language"] == "en"
    rel = load(pipe, "relations.jsonl")
    assert rel == [{"from_source_id": kept["/en/law/1"]["source_id"], "to_source_id": law["source_id"],
                    "relation": "translation_of", "evidence": law["group_key"]}]

    # Landing page: the notice only introduces its PDF, so the PDF is the record and inherits title and date.
    assert excluded["/kh/notice/7"]["reason"] == "landing_page"
    rep = kept["/files/report.pdf"]
    assert rep["title"].startswith("សេចក្តីជូនដំណឹង") or rep["title"] == "Annual report 2025"
    assert rep["landing_url"] == site.base + "/kh/notice/7" and rep["language"] == "en" and rep["is_scanned"] is False

    # Deduplication: the byte-identical copy is dropped and remembered as an alternate URL.
    assert excluded["/files/copy-of-report.pdf"]["reason"] == "duplicate:exact_checksum"
    assert rep["alternate_urls"] == [site.base + "/files/copy-of-report.pdf"]

    # Exclusions and failure handling.
    assert excluded["/kh/notice/8"]["reason"] == "personal_data"
    assert excluded["/kh/notice/9"]["reason"] == "thin_content"
    assert state["/private/secret"] == "robots_blocked" and site.hits.get("/private/secret") is None
    assert state["/files/missing.pdf"] == "gone" and state["/files/fake.pdf"] == "skipped"
    assert not any("elsewhere.example" in u or "facebook" in u for u in state)
    assert site.hits["/files/flaky.pdf"] == 3  # two 503s, then success
    assert state["/docs?page=2"] == "done"  # pagination followed

    scan = kept["/files/scan.pdf"]
    assert scan["is_scanned"] is True and scan["needs_ocr"] is True
    assert (scan["doc_type"], scan["doc_number"], scan["language"]) == ("legal_instrument", "098 សហវ.ប្រក", "km")
    post = kept["/post/1"]
    assert post["is_press"] is True and post["issue_date"] == "2026-10-02" and site.hits.get("/post/1") is None  # body came from the API

    assert report["acceptance"]["meets_target"] and report["acceptance"]["required_fields_complete"]
    assert report["funnel"]["retained"] == 6 and report["funnel"]["deduplicated"] == 1
    assert (pipe.data / "reports" / "corpus_report.md").exists() and (pipe.data / "manifest" / "manifest.csv").exists()
    assert {c["change"] for c in load(pipe, "changes.jsonl")} == {"added"}


def test_resume_is_idempotent(pipe, site):
    pipe.discover()
    pipe.fetch(limit=2)  # an interrupted run
    partial = pipe.db.count("state='done' AND kind IN ('page','file')")
    assert 2 <= partial < 8
    pipe.discover()
    pipe.fetch()
    first = finish(pipe)["funnel"]["retained"]
    hits = dict(site.hits)
    pipe.discover()
    pipe.fetch()  # nothing left to do
    assert finish(pipe)["funnel"]["retained"] == first == 6
    assert site.hits.get("/files/report.pdf") == hits.get("/files/report.pdf")
    assert load(pipe, "changes.jsonl") == []


def test_incremental_recrawl(pipe, site):
    pipe.discover()
    pipe.fetch()
    finish(pipe)
    before = {m["canonical_url"]: m for m in load(pipe, "manifest.jsonl")}

    # Unchanged site: conditional requests, nothing re-downloaded, no changes reported.
    pipe.schedule_recrawl(days=0)
    pipe.discover()
    pipe.fetch()
    finish(pipe)
    assert load(pipe, "changes.jsonl") == []
    row = pipe.db.one("SELECT http_status FROM urls WHERE url=?", (site.base + "/files/report.pdf",))
    assert row["http_status"] == 304

    # The site publishes a new post and replaces one PDF.
    site.use_v2, site.extra_post = True, True
    pipe.schedule_recrawl(days=0)
    pipe.discover()
    pipe.fetch()
    finish(pipe)
    after = {m["canonical_url"]: m for m in load(pipe, "manifest.jsonl")}
    changes = {c["source_id"]: c["change"] for c in load(pipe, "changes.jsonl")}
    report_url = site.base + "/files/report.pdf"
    assert after[report_url]["checksum"] != before[report_url]["checksum"]
    assert changes[after[report_url]["source_id"]] == "changed"
    assert changes[after[site.base + "/post/2"]["source_id"]] == "added"
    # The former duplicate now differs from the revised report, so it is its own record again.
    assert changes[after[site.base + "/files/copy-of-report.pdf"]["source_id"]] == "added"
    assert len(changes) == 3
    assert pipe.db.count("url=?", (report_url,), table="versions") == 2
