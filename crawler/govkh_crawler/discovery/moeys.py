"""Ministry of Education, Youth and Sport: the site is client-rendered, but its own JSON API
lists every legal document with a direct file path."""
from __future__ import annotations

from typing import Iterator
from urllib.parse import urljoin

from ..classify import FAMILY_OF, extract_date, extract_doc_number
from ..urlnorm import canonicalize, drive_download
from .base import Adapter, Candidate, log

SUBTYPE = {
    "royal-decree": "royal_decree", "sub-decree": "sub_decree", "decision": "decision",
    "circular": "circular", "instructions": "instruction", "declaration": "prakas",
    "press-release": "press_release", "statement": "press_release", "ann": "notification",
}


def _s(value) -> str:
    return value.strip() if isinstance(value, str) else ""


class MoeysAdapter(Adapter):
    """cfg: api (category endpoint URL), file_base (prefix for document_file paths)."""

    type = "moeys"

    def _categories(self) -> list[dict]:
        data, _, _, error = self.get_json(self.cfg["api"])
        if error or not isinstance(data, dict):
            log.warning("[%s] API %s: %s", self.src.id, self.cfg["api"], error or "unexpected response")
            return []
        return data.get("category") or []

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        n = 0
        for cat in self._categories():
            sub = SUBTYPE.get(cat.get("slug", ""), "other")
            fam = FAMILY_OF.get(sub, "legal_instrument")
            for doc in cat.get("documents") or []:
                path, link = (_s(doc.get("document_file")), _s(doc.get("link")))
                if path:
                    url = canonicalize(urljoin(self.cfg["file_base"], path))
                else:
                    url = canonicalize(link)
                    url = (drive_download(url) or url) if url else None
                if not url:
                    continue
                title = " ".join(_s(doc.get("title")).split())
                yield Candidate(url, "file", {
                    "title": title,
                    "doc_type": fam,
                    "doc_subtype": sub,
                    "doc_number": extract_doc_number(title),
                    "issue_date": extract_date(title),
                    "issue_date_source": "title",
                    "language_hint": "km",
                    "category": cat.get("slug"),
                })
                n += 1
                if limit and n >= limit:
                    return

    def estimate(self) -> tuple[int | None, str]:
        return sum(len(c.get("documents") or []) for c in self._categories()), "documents listed by the site's API"
