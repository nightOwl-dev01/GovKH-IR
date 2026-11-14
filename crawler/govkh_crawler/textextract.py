"""File-type sniffing and light text extraction.

The text is used for language detection, eligibility and near-duplicate detection. It is also
saved as a convenience for the indexing stage, which owns the real parsing and OCR.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

from .classify import GENERIC_ANCHOR
from .urlnorm import DOC_EXT, IMG_EXT, canonicalize, drive_download, extension

DOC_TYPES = DOC_EXT | {"html"}
IMAGE_TYPES = {"jpg", "png", "webp", "gif"}
MAIN_SELECTORS = (
    "main", "article", ".entry-content", ".post-content", ".article-content", ".single-content",
    ".content-detail", ".detail", "#content", ".content", "#main",
)
JUNK_IMG = re.compile(r"(logo|icon|avatar|banner|sprite|placeholder|loading|flag|emoji|button|bg[-_.]|background)", re.I)


def sniff(head: bytes, url: str, content_type: str = "") -> str:
    """Real file type from magic bytes, falling back to extension and Content-Type."""
    ext = extension(url)
    ct = (content_type or "").split(";")[0].strip().lower()
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        if ext in ("docx", "xlsx", "pptx"):
            return ext
        for needle, kind in ((b"word/", "docx"), (b"xl/", "xlsx"), (b"ppt/", "pptx")):
            if needle in head[:4096] or kind in ct or {"docx": "wordprocessing", "xlsx": "spreadsheet", "pptx": "presentation"}[kind] in ct:
                return kind
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        if ext in ("doc", "xls", "ppt"):
            return ext
        return "xls" if "excel" in ct else "ppt" if "powerpoint" in ct else "doc"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"GIF8"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    low = head[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if low.startswith((b"<!doctype html", b"<html")) or b"<html" in low or b"<body" in low or b"<head" in low:
        return "html"
    if "html" in ct:
        return "html"
    if "json" in ct or low.startswith((b"{", b"[")):
        return "json"
    if "xml" in ct or low.startswith(b"<?xml"):
        return "xml"
    if ct.startswith("text/"):
        return "txt"
    return "bin"


def sniff_path(path: Path, url: str, content_type: str = "") -> str:
    with path.open("rb") as f:
        kind = sniff(f.read(8192), url, content_type)
    if kind == "zip":  # OOXML whose part names sit beyond the first bytes
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
        except (zipfile.BadZipFile, OSError):
            return "zip"
        for prefix, ooxml in (("word/", "docx"), ("xl/", "xlsx"), ("ppt/", "pptx")):
            if any(n.startswith(prefix) for n in names):
                return ooxml
    return kind


def clean_text(text: str) -> str:
    text = text.replace(" ", " ").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


@dataclass
class HtmlDoc:
    title: str = ""
    text: str = ""
    links: list[tuple[str, str, bool]] = field(default_factory=list)  # url, anchor text, in main content
    images: list[str] = field(default_factory=list)  # content images (possible scanned notices)
    titles: list[str] = field(default_factory=list)  # title candidates, best first
    lang: str = ""


def parse_html(data: bytes, base_url: str) -> HtmlDoc:
    soup = BeautifulSoup(data, "html.parser")
    doc = HtmlDoc()
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        doc.lang = str(html_tag.get("lang")).lower()[:2]
    base = soup.find("base", href=True)
    if base:
        base_url = canonicalize(base["href"], base_url) or base_url

    main = None
    for sel in MAIN_SELECTORS:
        found = soup.select(sel)
        if found:
            best = max(found, key=lambda el: len(el.get_text(strip=True)))
            if len(best.get_text(strip=True)) >= 200:
                main = best
                break
    main = main or soup.body or soup  # no container with real content: judge the whole page
    chrome = ["nav", "header", "footer", "aside"]
    main_anchor_ids = {id(a) for a in main.find_all("a", href=True) if not a.find_parent(chrome)}

    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = canonicalize(href, base_url)
        url = drive_download(url) or url
        if not url or url in seen:
            continue
        seen.add(url)
        doc.links.append((url, _anchor_text(a), id(a) in main_anchor_ids))
    for tag in soup.find_all(["iframe", "embed", "object"]):  # embedded PDF viewers
        src = tag.get("src") or tag.get("data") or ""
        m = re.search(r"[?&](?:file|url|src)=([^&]+)", src)
        if m:
            from urllib.parse import unquote

            src = unquote(m.group(1))
        url = canonicalize(src, base_url)
        drive = drive_download(url)
        if url and (drive or extension(url) in DOC_EXT) and (drive or url) not in seen:
            seen.add(drive or url)
            doc.links.append((drive or url, "", True))

    for img in main.find_all("img"):
        if img.find_parent(chrome):
            continue
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src") or ""
        url = canonicalize(src, base_url)
        if not url or extension(url) not in IMG_EXT or JUNK_IMG.search(url):
            continue
        try:
            if int(str(img.get("width") or 9999).rstrip("px")) < 200:
                continue
        except ValueError:
            pass
        if url not in doc.images:
            doc.images.append(url)

    # Title candidates. Many sites put the site name in <h1>, so the caller drops any candidate
    # that repeats across the source's pages and takes the first one left.
    og = soup.find("meta", attrs={"property": "og:title"})
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    cands = [h.get_text(" ", strip=True) for h in soup.find_all("h1")[:4]]
    cands.append(og.get("content", "") if og else "")
    cands += [h.get_text(" ", strip=True) for h in soup.find_all(["h2", "h3"])[:12]]
    cands += sorted(re.split(r"\s+[|–—-]\s+", page_title), key=len, reverse=True)
    for c in cands:
        c = clean_text(c)[:500]
        if len(c) >= 4 and c not in doc.titles:
            doc.titles.append(c)
    doc.title = doc.titles[0] if doc.titles else ""

    for tag in main.find_all(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "button"]):
        tag.decompose()
    doc.text = clean_text(main.get_text("\n", strip=True))
    return doc


def _anchor_text(a) -> str:
    """Link text; for a generic label ("KH", "Download") the text of the row or item around it."""
    text = a.get_text(" ", strip=True)
    if len(text) >= 12 and not GENERIC_ANCHOR.match(text):
        return text[:300]
    for depth, parent in enumerate(a.parents):
        if depth > 6 or parent.name in ("body", "html", "ul", "ol", "table", "tbody", "nav"):
            break
        if parent.name in ("tr", "li", "article", "div", "section", "p", "td"):
            ctx = clean_text(parent.get_text(" ", strip=True))
            if len(ctx) > 400:
                break
            if len(ctx) >= 12:
                return ctx[:300]
    return text[:300]


@dataclass
class Extracted:
    text: str = ""
    page_count: int | None = None
    is_scanned: bool | None = None
    error: str = ""


def extract_pdf(path: Path, scanned_chars_per_page: int = 50, max_pages: int = 400) -> Extracted:
    try:
        import pymupdf as fitz
    except ImportError:
        return Extracted(error="pymupdf_missing")
    try:
        with fitz.open(path) as pdf:
            if pdf.needs_pass:
                return Extracted(error="encrypted")
            n = pdf.page_count
            if n == 0:
                return Extracted(error="corrupt")
            parts = [pdf[i].get_text("text") for i in range(min(n, max_pages))]
    except Exception as e:  # PyMuPDF raises several unrelated types on damaged files
        return Extracted(error=f"corrupt:{type(e).__name__}")
    text = clean_text("\n".join(parts))
    letters = sum(1 for ch in text if ch.isalnum())
    return Extracted(text=text, page_count=n, is_scanned=letters / min(n, max_pages) < scanned_chars_per_page)


def extract_ooxml(path: Path, kind: str) -> Extracted:
    """Text of a .docx / .xlsx / .pptx without extra dependencies."""
    prefix = {"docx": "word/document", "xlsx": "xl/sharedStrings", "pptx": "ppt/slides/slide"}[kind]
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.startswith(prefix) and n.endswith(".xml")]
            xml = "\n".join(z.read(n).decode("utf-8", "replace") for n in sorted(names))
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        return Extracted(error=f"corrupt:{type(e).__name__}")
    xml = re.sub(r"</w:p>|</a:p>|</si>|<w:br[^>]*>", "\n", xml)
    import html

    return Extracted(text=clean_text(html.unescape(re.sub(r"<[^>]+>", "", xml))))


def extract(path: Path, file_type: str, base_url: str = "", scanned_chars_per_page: int = 50) -> tuple[Extracted, HtmlDoc | None]:
    if file_type == "html":
        doc = parse_html(path.read_bytes(), base_url)
        return Extracted(text=doc.text), doc
    if file_type == "pdf":
        return extract_pdf(path, scanned_chars_per_page), None
    if file_type in ("docx", "xlsx", "pptx"):
        return extract_ooxml(path, file_type), None
    return Extracted(), None  # legacy .doc/.xls/.ppt: left to the parsing stage
