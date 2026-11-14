"""URL canonicalisation, so one document has exactly one frontier key."""
from __future__ import annotations

import posixpath
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid|_ga|ref|share|PHPSESSID|sid)$", re.I)
DOC_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx"}
IMG_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
SKIP_EXT = {
    "css", "js", "json", "xml", "rss", "ico", "svg", "woff", "woff2", "ttf", "eot", "map",
    "mp3", "mp4", "avi", "mov", "wmv", "webm", "m4a", "wav", "zip", "rar", "7z", "gz", "apk", "exe",
}
SOCIAL = re.compile(
    r"(^|\.)(facebook|fb|instagram|twitter|x|tiktok|youtube|youtu|t|telegram|linkedin|whatsapp)\.(com|me|be)$"
)


def canonicalize(url: str, base: str | None = None) -> str | None:
    """Return the canonical form of an http(s) URL, or None if it is not crawlable."""
    if not url:
        return None
    url = url.strip().replace("\\", "/")
    if base:
        url = urljoin(base, url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname.lower().rstrip(".")
    try:
        port = parts.port
    except ValueError:
        return None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    trailing = path.endswith("/")
    path = posixpath.normpath(path) if path != "/" else "/"
    if trailing and path != "/":
        path += "/"
    # One consistent percent-encoding (Khmer paths appear both raw and encoded).
    path = quote(unquote(path), safe="/:@!$&'()*+,;=~-._")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)]
    return urlunsplit((scheme, host, path, urlencode(query, quote_via=quote, safe="@:/"), ""))


def extension(url: str) -> str:
    path = unquote(urlsplit(url).path)
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def filename(url: str) -> str:
    return unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1]


DRIVE_HOSTS = ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")
_DRIVE_ID = re.compile(r"/(file|document|spreadsheets|presentation)/d/([\w-]{10,})")


def drive_download(url: str | None) -> str | None:
    """Direct-download URL of a public Google Drive / Docs file, or None if `url` is not one."""
    if not url:
        return None
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in DRIVE_HOSTS:
        return None
    m = _DRIVE_ID.search(parts.path)
    kind, fid = (m.group(1), m.group(2)) if m else ("file", dict(parse_qsl(parts.query)).get("id", ""))
    if not re.fullmatch(r"[\w-]{10,}", fid or "") or "/folders/" in parts.path:
        return None
    if kind == "document":
        return f"https://docs.google.com/document/d/{fid}/export?format=pdf"
    if kind == "spreadsheets":
        return f"https://docs.google.com/spreadsheets/d/{fid}/export?format=xlsx"
    if kind == "presentation":
        return f"https://docs.google.com/presentation/d/{fid}/export/pdf"
    return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"


def is_drive_file(url: str) -> bool:
    return drive_download(url) == url


def is_social(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return bool(SOCIAL.search(host))
