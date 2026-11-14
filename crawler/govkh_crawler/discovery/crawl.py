"""Focused crawl from document listing pages, for sites without a sitemap or an API.

The adapter only seeds the start URLs. Link expansion happens while fetching
(`Pipeline._expand`), governed by this adapter's rules:

  start_urls         listing pages to begin from
  allow / deny       regexes a followed link must / must not match
  doc_page_patterns  regexes of HTML pages that ARE documents (stored as records);
                     without them only linked files (PDF, Office) become records
  pagination         regexes of "next page" links, which do not consume crawl depth
  max_depth          link depth from a start URL (default 4)
  max_pages          cap on listing pages visited for this source (default 3000)
"""
from __future__ import annotations

from typing import Iterator

from ..urlnorm import canonicalize
from .base import Adapter, Candidate


class CrawlAdapter(Adapter):
    type = "crawl"

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        for start in self.cfg.get("start_urls", []):
            url = canonicalize(start)
            if url and self.src.owns(url):
                yield Candidate(url, "listing")
