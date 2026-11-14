"""Settings and the source registry (the whitelist of official domains)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = ROOT / "config"

DOC_FAMILIES = ("legal_instrument", "announcement", "procedure", "report", "form_fee")


@dataclass
class Settings:
    bot_name: str = "GovKHSearchBot"
    bot_version: str = "0.1"
    bot_description: str = "academic IR course project; polite crawler"
    contact: str = ""  # e-mail or URL shown to site operators; set it before a full crawl
    data_dir: str = "data"
    default_delay: float = 1.0  # seconds between requests to one host
    max_crawl_delay: float = 30.0  # cap on robots.txt Crawl-delay
    timeout: float = 45.0
    max_retries: int = 4  # per request, with exponential backoff
    backoff_base: float = 2.0
    max_backoff: float = 120.0
    max_bytes: int = 100 * 1024 * 1024  # oversized files are skipped and logged
    max_attempts: int = 5  # per URL, across runs
    host_fail_limit: int = 8  # consecutive failures before a host is paused for the run
    recrawl_days: int = 7
    thin_text_chars: int = 300  # an HTML page below this is "thin"
    landing_text_chars: int = 600  # a page below this with attachments is only a landing page
    scanned_chars_per_page: int = 50  # PDF pages below this are treated as scanned
    near_dup_threshold: float = 0.9
    include_press: bool = True  # see README: press releases are decided after the audit
    download_page_images: bool = True  # images of thin pages, kept as OCR assets
    max_page_images: int = 10
    respect_robots: bool = True
    # Public files that official pages embed from Google Drive/Docs are downloaded once each.
    # Drive's robots.txt disallows its download endpoint for crawlers; set false to honour that.
    fetch_embedded_files: bool = True
    embedded_file_hosts: list[str] = field(
        default_factory=lambda: ["drive.google.com", "docs.google.com", "drive.usercontent.google.com"]
    )
    render_settle_ms: int = 10000
    verify_tls: bool = True
    corpus_target: int = 10001
    planning_targets: dict[str, int] = field(
        default_factory=lambda: {
            "legal_instrument": 4200,
            "announcement": 3000,
            "procedure": 1800,
            "report": 1800,
            "form_fee": 1200,
        }
    )

    @property
    def user_agent(self) -> str:
        extra = f"; {self.contact}" if self.contact else ""
        return f"{self.bot_name}/{self.bot_version} ({self.bot_description}{extra})"

    def data_path(self, base: Path = ROOT) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else base / p


def _host_of(url_or_host: str) -> str:
    h = urlsplit(url_or_host).netloc if "//" in url_or_host else url_or_host
    h = h.lower()
    return h[4:] if h.startswith("www.") else h


@dataclass
class Source:
    id: str
    institution: str
    domains: list[str]
    institution_km: str = ""
    enabled: bool = True
    priority: int = 10  # higher wins when duplicates are merged (issuer site > archive)
    press: bool = False  # content is mostly press releases / activity news
    default_doc_type: str = "announcement"
    default_language: str = ""
    reuse_status: str = "unclear"  # open | unclear | restricted
    delay: float | None = None
    max_docs: int | None = None
    verify_tls: bool | None = None
    page_images: bool | None = None  # override settings.download_page_images for this source
    render: bool = False  # fetch listing and document pages with headless Chromium
    extra_file_hosts: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)  # URL regexes never fetched
    file_url_patterns: list[str] = field(default_factory=list)  # extension-less download URLs
    translation_url_pattern: str = ""  # e.g. "/(kh|km|en)/": links language versions
    adapters: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        self._domains = [_host_of(d) for d in self.domains]
        self._file_hosts = self._domains + [_host_of(d) for d in self.extra_file_hosts]
        self._exclude = [re.compile(p) for p in self.exclude]
        self._file_pats = [re.compile(p) for p in self.file_url_patterns]
        self._tr = re.compile(self.translation_url_pattern) if self.translation_url_pattern else None

    @staticmethod
    def _match(host: str, allowed: list[str]) -> bool:
        host = _host_of(host)
        return any(host == d or host.endswith("." + d) for d in allowed)

    def owns(self, url: str) -> bool:
        """True when the URL is on one of the source's own (crawlable) domains."""
        return self._match(urlsplit(url).netloc, self._domains)

    def allows(self, url: str) -> bool:
        """True when the URL may be downloaded for this source (own domains + file hosts)."""
        return self._match(urlsplit(url).netloc, self._file_hosts)

    def excluded(self, url: str) -> bool:
        return any(p.search(url) for p in self._exclude)

    def is_file_url(self, url: str) -> bool:
        return any(p.search(url) for p in self._file_pats)

    def translation_key(self, url: str) -> tuple[str, str] | None:
        """(group key, language) when the URL carries a language segment."""
        if not self._tr:
            return None
        m = self._tr.search(url)
        if not m:
            return None
        seg = (m.group(1) if m.groups() else m.group(0)).lower()
        lang = "km" if seg.startswith(("kh", "km")) else "en" if seg.startswith("en") else ""
        return url[: m.start()] + "/*/" + url[m.end():], lang

    @property
    def crawl(self) -> dict[str, Any] | None:
        for a in self.adapters:
            if a.get("type") == "crawl":
                return a
        return None


def load_settings(path: Path | None = None) -> Settings:
    path = path or DEFAULT_CONFIG_DIR / "settings.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    known = {f.name for f in fields(Settings)}
    unknown = set(raw or {}) - known
    if unknown:
        raise ValueError(f"unknown settings keys: {sorted(unknown)}")
    return Settings(**(raw or {}))


def load_sources(path: Path | None = None) -> dict[str, Source]:
    path = path or DEFAULT_CONFIG_DIR / "sources.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, Source] = {}
    for item in raw.get("sources", []):
        src = Source(**item)
        if src.id in out:
            raise ValueError(f"duplicate source id: {src.id}")
        if src.default_doc_type not in DOC_FAMILIES:
            raise ValueError(f"{src.id}: default_doc_type must be one of {DOC_FAMILIES}")
        out[src.id] = src
    return out
