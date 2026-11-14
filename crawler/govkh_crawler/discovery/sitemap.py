"""XML sitemaps (plain, index, gzip). Re-read on every run; <lastmod> drives incremental refetch."""
from __future__ import annotations

import gzip
import re
import xml.etree.ElementTree as ET
from typing import Iterator
from urllib.parse import urlsplit

from ..urlnorm import DOC_EXT, SKIP_EXT, canonicalize, extension
from .base import Adapter, Candidate, log

GUESSES = ("/sitemap.xml", "/wp-sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(data: bytes) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (child sitemap URLs, [(page URL, lastmod)])."""
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError:
            return [], []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return [], []
    children, urls = [], []
    for node in root:
        loc = lastmod = ""
        for el in node:
            name = _local(el.tag)
            if name == "loc":
                loc = (el.text or "").strip()
            elif name == "lastmod":
                lastmod = (el.text or "").strip()
        if not loc:
            continue
        if _local(node.tag) == "sitemap":
            children.append(loc)
        elif _local(node.tag) == "url":
            urls.append((loc, lastmod))
    return children, urls


class SitemapAdapter(Adapter):
    """cfg: urls (optional), include/exclude (page URL regexes), sitemap_exclude, kind, max_sitemaps."""

    type = "sitemap"

    def _roots(self) -> list[str]:
        if self.cfg.get("urls"):
            return list(self.cfg["urls"])
        roots: list[str] = []
        for domain in self.src.domains:
            origin = domain if "//" in domain else f"https://{domain}"
            rb = self.client.robots(origin + "/", self.src.verify_tls)
            roots += rb.sitemaps or [origin + g for g in GUESSES]
        return roots

    def _walk(self, max_sitemaps: int) -> Iterator[tuple[str, str]]:
        skip = [re.compile(p) for p in self.cfg.get("sitemap_exclude", [r"taxonom", r"users", r"author", r"tag"])]
        queue, seen, found_any = self._roots(), set(), False
        guessed = not self.cfg.get("urls")
        while queue and len(seen) < max_sitemaps:
            sm = queue.pop(0)
            if sm in seen or any(p.search(sm) for p in skip):
                continue
            seen.add(sm)
            if not self.src.owns(sm):
                continue
            status, body, _, error = self.get(sm)
            if status != 200 or error:
                if not guessed:
                    log.warning("[%s] sitemap %s: %s", self.src.id, sm, error or status)
                continue
            children, urls = parse_sitemap(body)
            if children or urls:
                found_any = True
            queue += children
            yield from urls
        if not found_any:
            log.warning("[%s] no usable sitemap found", self.src.id)

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        kind = self.cfg.get("kind", "page")
        n = 0
        for loc, lastmod in self._walk(int(self.cfg.get("max_sitemaps", 300))):
            url = canonicalize(loc)
            if not url or not self.src.owns(url) or not self.wanted(url):
                continue
            ext = extension(url)
            if ext in SKIP_EXT or (urlsplit(url).path in ("", "/") and not urlsplit(url).query):
                continue
            hints = {"modified": lastmod[:19]} if lastmod else {}
            yield Candidate(url, "file" if ext in DOC_EXT else kind, hints)
            n += 1
            if limit and n >= limit:
                return

    def estimate(self) -> tuple[int | None, str]:
        cap = int(self.cfg.get("estimate_sitemaps", 12))
        n = sum(1 for loc, _ in self._walk(cap) if (u := canonicalize(loc)) and self.wanted(u))
        return n, f"URLs matching filters in the first {cap} sitemap file(s)"
