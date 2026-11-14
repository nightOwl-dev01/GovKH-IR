"""Discovery adapters turn one access method (sitemap, API, listing pages) into frontier URLs."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..config import Settings, Source
from ..db import DB
from ..http import HostBlocked, PoliteClient

log = logging.getLogger("govkh.discovery")


@dataclass
class Candidate:
    url: str
    kind: str  # listing | page | file
    hints: dict[str, Any] = field(default_factory=dict)
    parent_url: str | None = None
    inline: bytes | None = None  # document body already obtained from an API (saves a request)


class Adapter:
    type = ""

    def __init__(self, settings: Settings, db: DB, client: PoliteClient, src: Source, cfg: dict[str, Any]):
        self.s, self.db, self.client, self.src, self.cfg = settings, db, client, src, cfg
        self._inc = [re.compile(p) for p in cfg.get("include", [])]
        self._exc = [re.compile(p) for p in cfg.get("exclude", [])]

    # -- helpers ----------------------------------------------------------
    def wanted(self, url: str) -> bool:
        if self.src.excluded(url) or any(p.search(url) for p in self._exc):
            return False
        return not self._inc or any(p.search(url) for p in self._inc)

    def get(self, url: str, **kw) -> tuple[int, bytes, dict[str, str], str]:
        kw.setdefault("retries", 2)
        return self.client.get(url, delay=self.src.delay, verify=self.src.verify_tls, **kw)

    def get_json(self, url: str) -> tuple[Any, dict[str, str], int, str]:
        status, body, headers, error = self.get(url, accept="application/json")
        if status != 200 or error:
            try:  # WordPress and CKAN describe errors in a JSON body
                return json.loads(body) if body else None, headers, status, error or f"http_{status}"
            except ValueError:
                return None, headers, status, error or f"http_{status}"
        try:
            return json.loads(body), headers, status, ""
        except ValueError:
            return None, headers, status, "invalid_json"

    # -- interface --------------------------------------------------------
    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        raise NotImplementedError

    def estimate(self) -> tuple[int | None, str]:
        """(estimated document count, note) using as few requests as possible."""
        return None, "not estimable without crawling"


__all__ = ["Adapter", "Candidate", "HostBlocked", "log"]
