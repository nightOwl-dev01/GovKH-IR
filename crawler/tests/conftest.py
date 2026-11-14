"""A small fake government website served on localhost, so the pipeline is tested end to end offline."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from govkh_crawler.config import Settings, Source  # noqa: E402
from govkh_crawler.pipeline import Pipeline  # noqa: E402

KM_LAW = "ច្បាប់ស្តីពីអត្តសញ្ញាណប័ណ្ណសញ្ជាតិខ្មែរ " + "មាត្រា ១ ច្បាប់នេះមានគោលបំណងកំណត់អំពីការផ្តល់អត្តសញ្ញាណប័ណ្ណសញ្ជាតិខ្មែរ។ " * 30
EN_REPORT = "Annual report on public service delivery. " + "The ministry processed applications within the legal time limit. " * 30


def make_pdf(text: str, font: str = "helv") -> bytes:
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(40, 40, 550, 800), text, fontsize=9, fontname=font)
    data = doc.tobytes()
    doc.close()
    return data


def page(title: str, body: str, lang: str = "km") -> bytes:
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><title>{title}</title></head>'
            f"<body><nav><a href='/'>Home</a><a href='https://www.facebook.com/ministry'>FB</a></nav>"
            f"<main><h1>{title}</h1>{body}</main><footer>footer text</footer></body></html>").encode("utf-8")


class Site:
    """Routes: path -> (status, content-type, body) or a callable(handler) for dynamic behaviour."""

    def __init__(self) -> None:
        self.hits: dict[str, int] = {}
        self.flaky_left = 2
        self.report_pdf = make_pdf(EN_REPORT)
        self.report_v2 = make_pdf(EN_REPORT + " Addendum: revised figures for the fourth quarter.")
        self.use_v2 = False
        self.extra_post = False
        self.routes = {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nDisallow: /private/\nSitemap: http://HOST/sitemap.xml\n"),
            "/sitemap.xml": self.sitemap,
            "/kh/law/1": (200, "text/html", page("ច្បាប់ស្តីពីអត្តសញ្ញាណប័ណ្ណសញ្ជាតិខ្មែរ លេខ ១២៣ នស/រកម", f"<p>{KM_LAW}</p>")),
            "/en/law/1": (200, "text/html", page("Law on Khmer Identity Cards No. 123", "<p>" + "This law determines the issuance of Khmer identity cards to citizens. " * 30 + "</p>", "en")),
            # Short post that only introduces its attachment -> landing page; the PDF is the document.
            "/kh/notice/7": (200, "text/html", page("សេចក្តីជូនដំណឹង ស្តីពីថ្ងៃឈប់សម្រាក ថ្ងៃទី១១ ខែវិច្ឆិកា ឆ្នាំ២០២៦", "<p>សូមទាញយក</p><a href='/files/report.pdf'>ទាញយក</a>")),
            "/kh/notice/8": (200, "text/html", page("បញ្ជីឈ្មោះបេក្ខជនជាប់ការប្រឡង", "<p>" + "បេក្ខជន ជាប់ " * 100 + "</p>")),
            "/kh/notice/9": (200, "text/html", page("ទំព័រទទេ", "<p>ខ្លី</p>")),
            "/private/secret": (200, "text/html", page("Secret", "<p>" + "secret " * 200 + "</p>")),
            "/docs": (200, "text/html", page("Documents", "<a href='/docs?page=2'>next</a><a href='/files/report.pdf'>Annual report 2025</a>"
                                             "<a href='/files/copy-of-report.pdf'>Annual report 2025 (copy)</a><a href='/files/flaky.pdf'>Budget report</a>"
                                             "<a href='/files/missing.pdf'>Missing</a><a href='/files/fake.pdf'>Fake</a><a href='https://elsewhere.example/x.pdf'>Offsite</a>")),
            "/docs?page=2": (200, "text/html", page("Documents 2", "<a href='/files/scan.pdf'>ប្រកាសលេខ ០៩៨ សហវ.ប្រក ស្តីពីពន្ធ</a>")),
            "/files/report.pdf": self.report,
            "/files/copy-of-report.pdf": lambda h: (200, "application/pdf", self.report_pdf),
            "/files/flaky.pdf": self.flaky,
            "/files/fake.pdf": (200, "text/html", page("Login required", "<p>Please log in</p>")),
            "/files/scan.pdf": (200, "application/pdf", make_pdf("")),
            "/wp-json/wp/v2/posts": self.wp_posts,
            "/wp-json/wp/v2/media": (200, "application/json", b"[]"),
        }

    def sitemap(self, h):
        urls = ["/kh/law/1", "/en/law/1", "/kh/notice/7", "/kh/notice/8", "/kh/notice/9", "/private/secret"]
        body = "<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>" + "".join(
            f"<url><loc>http://{h.headers['Host']}{u}</loc><lastmod>2026-11-01T00:00:00+07:00</lastmod></url>" for u in urls) + "</urlset>"
        return 200, "application/xml", body.encode()

    def report(self, h):
        body, etag = (self.report_v2, '"v2"') if self.use_v2 else (self.report_pdf, '"v1"')
        if h.headers.get("If-None-Match") == etag:
            return 304, "application/pdf", b"", {"ETag": etag}
        return 200, "application/pdf", body, {"ETag": etag}

    def flaky(self, h):
        if self.flaky_left > 0:
            self.flaky_left -= 1
            return 503, "text/plain", b"busy", {"Retry-After": "0"}
        return 200, "application/pdf", make_pdf("Budget report. " + "Budget execution by programme and quarter. " * 40)

    def wp_posts(self, h):
        if "offset=0" not in h.path:
            return 200, "application/json", b"[]"
        host = h.headers["Host"]
        posts = [{"id": 1, "date": "2026-10-02T09:00:00", "modified_gmt": "2026-10-02T02:00:00", "link": f"http://{host}/post/1",
                  "title": {"rendered": "សេចក្តីប្រកាសព័ត៌មាន ស្តីពីសេវាសាធារណៈ"},
                  "content": {"rendered": "<p>" + "ក្រសួងសូមជម្រាបជូនសាធារណជនអំពីការផ្តល់សេវាសាធារណៈថ្មី។ " * 20 + "</p>"}, "categories": [1]}]
        if self.extra_post:
            posts.append({"id": 2, "date": "2026-11-10T09:00:00", "modified_gmt": "2026-11-10T02:00:00", "link": f"http://{host}/post/2",
                          "title": {"rendered": "Notification on new office hours"},
                          "content": {"rendered": "<p>" + "The office opens at eight in the morning on working days. " * 20 + "</p>"}, "categories": [1]})
        if "modified_after=" in h.path:
            posts = [p for p in posts if p["id"] == 2]
        return 200, "application/json", json.dumps(posts).encode(), {"X-WP-Total": str(len(posts)), "X-WP-TotalPages": "1"}


@pytest.fixture()
def site():
    s = Site()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            s.hits[self.path] = s.hits.get(self.path, 0) + 1
            key = self.path if self.path in s.routes else self.path.split("?")[0]
            route = s.routes.get(key)
            if route is None:
                out = (404, "text/plain", b"not found")
            else:
                out = route(self) if callable(route) else route
            status, ctype, body, *rest = out
            body = body.replace(b"HOST", self.headers["Host"].encode()) if key == "/robots.txt" else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (rest[0] if rest else {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    s.host = f"127.0.0.1:{server.server_port}"
    s.base = f"http://{s.host}"
    yield s
    server.shutdown()


@pytest.fixture()
def pipe(site, tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"), default_delay=0.0, backoff_base=0.01, max_backoff=0.05, corpus_target=3)
    sources = {
        "ministry": Source(
            id="ministry", institution="Ministry of Testing", institution_km="ក្រសួងសាកល្បង", domains=[site.base],
            default_doc_type="announcement", priority=10, translation_url_pattern="/(kh|en)/",
            adapters=[
                {"type": "sitemap"},
                {"type": "crawl", "start_urls": [f"{site.base}/docs"], "max_depth": 2},
                {"type": "wordpress", "apis": [{"url": f"{site.base}/wp-json/wp/v2", "language": "km"}]},
            ],
        )
    }
    p = Pipeline(settings, sources, tmp_path)
    yield p
    p.close()
