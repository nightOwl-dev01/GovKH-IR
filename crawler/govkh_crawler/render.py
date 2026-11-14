"""Headless Chromium for sites that build their pages with JavaScript.

Playwright's sync API is bound to the thread that started it, so each crawl thread gets its own
browser (created on first use, closed when the thread finishes). The browser identifies itself with
the crawler's User-Agent; there is no fingerprint spoofing and no challenge solving.
"""
from __future__ import annotations

import threading


class Renderer:
    def __init__(self, user_agent: str):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._ctx = self._browser.new_context(user_agent=user_agent, locale="km-KH")
        # Images, fonts and media are not needed to read the page.
        self._ctx.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )
        self._page = self._ctx.new_page()

    def render(self, url: str, timeout_s: float, settle_ms: int = 10000) -> tuple[int, str, dict[str, str], bytes]:
        """(status, final URL, headers, rendered HTML). Raises on navigation errors."""
        from playwright.sync_api import TimeoutError as PWTimeout

        resp = self._page.goto(url, wait_until="load", timeout=timeout_s * 1000)
        try:
            self._page.wait_for_load_state("networkidle", timeout=settle_ms)
        except PWTimeout:
            pass  # pages with polling widgets never go idle; what has loaded is enough
        headers = {k.lower(): v for k, v in (resp.headers if resp else {}).items()}
        headers["content-type"] = "text/html; charset=utf-8"
        headers.pop("content-length", None)
        headers.pop("etag", None)  # the rendered DOM is not the resource the ETag describes
        headers.pop("last-modified", None)
        return (resp.status if resp else 0), self._page.url, headers, self._page.content().encode("utf-8")

    def close(self) -> None:
        for closer in (self._ctx.close, self._browser.close, self._pw.stop):
            try:
                closer()
            except Exception:
                pass


class RendererPool:
    """One Renderer per thread."""

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self._tl = threading.local()

    def get(self) -> Renderer:
        r = getattr(self._tl, "renderer", None)
        if r is None:
            r = self._tl.renderer = Renderer(self.user_agent)
        return r

    def close_current(self) -> None:
        r = getattr(self._tl, "renderer", None)
        if r is not None:
            r.close()
            self._tl.renderer = None
