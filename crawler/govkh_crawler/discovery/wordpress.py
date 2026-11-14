"""WordPress REST API: 100 posts per request instead of one request per page."""
from __future__ import annotations

import html
import re
from typing import Iterator
from urllib.parse import quote, urlencode

from ..classify import good_title, iso_date
from ..urlnorm import canonicalize
from .base import Adapter, Candidate, log

_TAGS = re.compile(r"<[^>]+>")


def _plain(rendered: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub("", rendered or ""))).strip()


def inline_html(title: str, content: str, link: str, date: str) -> bytes:
    """The post exactly as the site's CMS renders its body, wrapped as a standalone document."""
    return (
        '<!doctype html>\n<html><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title>"
        f'<link rel="canonical" href="{html.escape(link, quote=True)}">'
        f'<meta name="date" content="{html.escape(date)}"></head>\n'
        f"<body><article><h1>{html.escape(title)}</h1>\n{content}\n</article></body></html>\n"
    ).encode("utf-8")


class WordPressAdapter(Adapter):
    """cfg:
    apis: [{url: https://site/wp-json/wp/v2, language: km}]
    types: [posts]            # also: pages, or custom post types exposed over REST
    content: inline | page    # inline = take the body from the API (default)
    media: true               # also list PDF/Office attachments from the media library
    categories / categories_exclude: [ids]
    """

    type = "wordpress"
    MEDIA_MIME = ("application/pdf", "application/msword",
                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  "application/vnd.ms-excel",
                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def _pages(self, api: str, route: str, params: dict[str, str], limit: int | None) -> Iterator[dict]:
        """Paginate in ascending `modified` order, resumable after a crash and incremental across runs."""
        key = f"wp:{api}/{route}:{params.get('mime_type', '')}"
        cur = self.db.get_cursor(self.src.id, key, {"since": None, "offset": 0, "max_mod": None})
        offset, since, max_mod = cur.get("offset", 0), cur["since"], cur["max_mod"]
        per_page = int(self.cfg.get("per_page", 50))
        if limit:
            per_page = min(per_page, max(5, limit))
        seen = 0
        while not self.client.stop.is_set():
            q = dict(params, per_page=str(per_page), offset=str(offset), orderby="modified", order="asc")
            if since:
                q["modified_after"] = since
            data, headers, status, error = self.get_json(f"{api}/{route}?{urlencode(q, quote_via=quote)}")
            if error or not isinstance(data, list):
                if per_page > 5 and (status == 0 or status >= 500):
                    per_page = max(5, per_page // 2)  # the server struggles with large batches: ask for less
                    log.info("[%s] %s/%s: %s, retrying with %d per request", self.src.id, api, route, error, per_page)
                    continue
                log.warning("[%s] %s/%s offset %s: %s", self.src.id, api, route, offset, error or "unexpected response")
                return  # keep the cursor: the next run resumes at this offset
            if not data:
                break
            for item in data:
                mod = item.get("modified_gmt") or item.get("modified") or ""
                if mod and (not max_mod or mod > max_mod):
                    max_mod = mod
                yield item
                seen += 1
            offset += len(data)
            self.db.set_cursor(self.src.id, key, {"since": since, "offset": offset, "max_mod": max_mod})
            if limit and seen >= limit:
                return  # pilot run: stop early, resume later from the saved offset
            total = int(headers.get("x-wp-total") or 0)
            if (total and offset >= total) or len(data) < per_page:
                break
        else:
            return
        # Finished a full pass: the next run only asks for items modified since then.
        self.db.set_cursor(self.src.id, key, {"since": max_mod or since, "offset": 0, "max_mod": max_mod})

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        inline = self.cfg.get("content", "inline") == "inline"
        for api_cfg in self.cfg["apis"]:
            api, lang = api_cfg["url"].rstrip("/"), api_cfg.get("language", "")
            for route in self.cfg.get("types", ["posts"]):
                params = {"_fields": "id,date,modified_gmt,link,title,content,categories"}
                if self.cfg.get("categories"):
                    params["categories"] = ",".join(map(str, self.cfg["categories"]))
                if self.cfg.get("categories_exclude"):
                    params["categories_exclude"] = ",".join(map(str, self.cfg["categories_exclude"]))
                for item in self._pages(api, route, params, limit):
                    url = canonicalize(item.get("link") or "")
                    if not url or not self.src.owns(url) or not self.wanted(url):
                        continue
                    title = _plain((item.get("title") or {}).get("rendered"))
                    date = item.get("date") or ""
                    hints = {
                        "title": title,
                        "issue_date": iso_date(date),
                        "issue_date_source": "published_date",
                        "modified": (item.get("modified_gmt") or "")[:19],
                        "language_hint": lang,
                        "wp_id": item.get("id"),
                    }
                    body = (item.get("content") or {}).get("rendered") or ""
                    yield Candidate(url, "page", hints, inline=inline_html(title, body, url, date) if inline and body.strip() else None)
            if self.cfg.get("media", True):
                for mime in self.MEDIA_MIME:
                    params = {"mime_type": mime, "_fields": "id,date,modified_gmt,source_url,title,post"}
                    for item in self._pages(api, "media", params, limit):
                        url = canonicalize(item.get("source_url") or "")
                        if not url or not self.src.allows(url) or not self.wanted(url):
                            continue
                        yield Candidate(url, "file", {
                            "anchor": good_title(_plain((item.get("title") or {}).get("rendered")).replace("-", " ").replace("_", " ")),
                            "issue_date": iso_date(item.get("date")),
                            "issue_date_source": "upload_date",
                            "modified": (item.get("modified_gmt") or "")[:19],
                            "language_hint": lang,
                        })

    def estimate(self) -> tuple[int | None, str]:
        total, parts = 0, []
        for api_cfg in self.cfg["apis"]:
            api = api_cfg["url"].rstrip("/")
            routes = [(r, "") for r in self.cfg.get("types", ["posts"])]
            if self.cfg.get("media", True):
                routes.append(("media", "&mime_type=application/pdf"))
            for route, extra in routes:
                _, headers, status, error = self.get_json(f"{api}/{route}?per_page=1&_fields=id{extra}")
                n = int(headers.get("x-wp-total") or 0) if not error else 0
                total += n
                parts.append(f"{route}{' (pdf)' if extra else ''}={n if not error else error}")
        return total, "WP REST X-WP-Total: " + ", ".join(parts)
