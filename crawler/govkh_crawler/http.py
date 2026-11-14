"""Polite HTTP client: per-host rate limit, robots.txt, retries with backoff, streamed downloads."""
from __future__ import annotations

import email.utils
import hashlib
import logging
import random
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .robots import Robots

log = logging.getLogger("govkh.http")

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
BLOCK_STATUS = {401, 403, 429}  # count toward the per-host circuit breaker


class HostBlocked(Exception):
    """The host refused or failed too many requests in a row; stop using it for this run."""


@dataclass
class FetchResult:
    url: str
    status: int = 0  # 0 = no HTTP response
    final_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    path: Path | None = None  # temp file holding the body (caller moves or deletes it)
    sha256: str = ""
    size: int = 0
    error: str = ""
    elapsed_ms: int = 0
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.path is not None and not self.error

    def read(self) -> bytes:
        data = self.path.read_bytes() if self.path else b""
        self.discard()
        return data

    def discard(self) -> None:
        if self.path:
            self.path.unlink(missing_ok=True)
            self.path = None


class _Host:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.fails = 0
        self.blocked = ""


class PoliteClient:
    def __init__(self, settings: Settings, tmp_dir: Path, stop: threading.Event | None = None):
        self.s = settings
        self.tmp_dir = tmp_dir
        tmp_dir.mkdir(parents=True, exist_ok=True)
        self.stop = stop or threading.Event()
        self._hosts: dict[str, _Host] = {}
        self._robots: dict[str, Robots] = {}
        self._guard = threading.Lock()
        self._clients: dict[bool, httpx.Client] = {}
        self.on_fetch = None  # optional callback(FetchResult), used for the fetch log
        from .render import RendererPool

        self.renderers = RendererPool(settings.user_agent)

    # -- plumbing ---------------------------------------------------------
    def _client(self, verify: bool) -> httpx.Client:
        with self._guard:
            if verify not in self._clients:
                self._clients[verify] = httpx.Client(
                    follow_redirects=True,
                    timeout=httpx.Timeout(self.s.timeout, connect=20.0),
                    verify=verify,
                    headers={
                        "User-Agent": self.s.user_agent,
                        "Accept": "text/html,application/xhtml+xml,application/pdf,application/json;q=0.9,*/*;q=0.8",
                        "Accept-Language": "km,en;q=0.8",
                    },
                    limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
                )
            return self._clients[verify]

    def close(self) -> None:
        for c in self._clients.values():
            c.close()

    def _host(self, url: str) -> _Host:
        key = urlsplit(url).netloc.lower()
        with self._guard:
            return self._hosts.setdefault(key, _Host())

    def blocked_hosts(self) -> dict[str, str]:
        return {h: st.blocked for h, st in self._hosts.items() if st.blocked}

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.stop.wait(seconds)

    # -- robots -----------------------------------------------------------
    def robots(self, url: str, verify: bool | None = None) -> Robots:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._guard:
            cached = self._robots.get(origin)
        if cached is not None:
            return cached
        res = self.fetch(origin + "/robots.txt", check_robots=False, verify=verify, max_bytes=1024 * 1024, retries=2)
        if res.ok:
            text = res.read().decode("utf-8", "replace")
            # A soft-404 HTML page is not a robots file.
            rb = Robots(status="missing") if "<html" in text[:2000].lower() else Robots.parse(text)
        elif 400 <= res.status < 500:
            res.discard()
            rb = Robots(status="missing")
        else:
            res.discard()
            rb = Robots(status="unreachable")
        with self._guard:
            self._robots[origin] = rb
        return rb

    def allowed(self, url: str, verify: bool | None = None) -> bool:
        if not self.s.respect_robots:
            return True
        return self.robots(url, verify).allowed(url, self.s.user_agent)

    # -- fetching ---------------------------------------------------------
    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        delay: float | None = None,
        check_robots: bool = True,
        verify: bool | None = None,
        max_bytes: int | None = None,
        retries: int | None = None,
        accept: str | None = None,
        render: bool = False,
    ) -> FetchResult:
        """GET `url` into a temp file. Never raises for HTTP/network errors; raises HostBlocked."""
        verify = self.s.verify_tls if verify is None else verify
        max_bytes = max_bytes or self.s.max_bytes
        retries = self.s.max_retries if retries is None else retries
        host = self._host(url)
        if host.blocked:
            raise HostBlocked(host.blocked)
        res = FetchResult(url=url)
        if check_robots and self.s.respect_robots:
            rb = self.robots(url, verify)
            if not rb.allowed(url, self.s.user_agent):
                res.error = "robots_unreachable" if rb.status == "unreachable" else "robots_disallowed"
                return res
            cd = rb.crawl_delay(self.s.user_agent)
            base_delay = max(delay or self.s.default_delay, min(cd or 0, self.s.max_crawl_delay))
        else:
            base_delay = delay or self.s.default_delay

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        if accept:
            headers["Accept"] = accept

        started = time.monotonic()
        with host.lock:  # one request at a time per host, spaced by the delay
            for attempt in range(1, retries + 2):
                if self.stop.is_set():
                    res.error = "interrupted"
                    break
                self._sleep(host.next_at - time.monotonic())
                res.attempts = attempt
                if render:
                    retry_after = self._render_once(url, res)
                    if res.error == "render:download":  # the URL is a file, not a page
                        retry_after = self._once(url, headers, verify, max_bytes, res)
                else:
                    retry_after = self._once(url, headers, verify, max_bytes, res)
                host.next_at = time.monotonic() + base_delay
                if not res.error and res.status not in RETRY_STATUS:
                    break
                if res.error in ("too_large",):
                    break
                if attempt > retries:
                    break
                wait = retry_after if retry_after is not None else self.s.backoff_base**attempt
                wait = min(self.s.max_backoff, wait) + random.uniform(0, 1)
                log.debug("retry %s in %.1fs (%s %s)", url, wait, res.status, res.error)
                host.next_at = max(host.next_at, time.monotonic() + wait)

            failed = bool(res.error and res.error not in ("too_large", "interrupted")) or res.status in BLOCK_STATUS or res.status >= 500
            if failed:
                host.fails += 1
                if host.fails >= self.s.host_fail_limit:
                    host.blocked = f"{host.fails} consecutive failures (last: {res.status or res.error})"
                    log.warning("host %s paused for this run: %s", urlsplit(url).netloc, host.blocked)
            elif res.status:
                host.fails = 0
        res.elapsed_ms = int((time.monotonic() - started) * 1000)
        if self.on_fetch:
            self.on_fetch(res)
        return res

    def _once(self, url: str, headers: dict[str, str], verify: bool, max_bytes: int, res: FetchResult) -> float | None:
        """One attempt. Fills `res`; returns Retry-After seconds when the server sent one."""
        res.status, res.error, res.sha256, res.size = 0, "", "", 0
        res.discard()
        tmp: Path | None = None
        try:
            with self._client(verify).stream("GET", url, headers=headers) as r:
                res.status = r.status_code
                res.final_url = str(r.url)
                res.headers = {k.lower(): v for k, v in r.headers.items()}
                if r.status_code != 200:
                    return _retry_after(res.headers.get("retry-after"))
                declared = int(res.headers.get("content-length") or 0)
                if declared > max_bytes:
                    res.error = "too_large"
                    return None
                h = hashlib.sha256()
                fd = tempfile.NamedTemporaryFile(dir=self.tmp_dir, delete=False, suffix=".part")
                tmp = Path(fd.name)
                with fd:
                    for chunk in r.iter_bytes(64 * 1024):
                        res.size += len(chunk)
                        if res.size > max_bytes:
                            res.error = "too_large"
                            break
                        h.update(chunk)
                        fd.write(chunk)
                if res.error:
                    tmp.unlink(missing_ok=True)
                    return None
                res.sha256, res.path = h.hexdigest(), tmp
        except httpx.TooManyRedirects:
            res.error = "too_many_redirects"
        except httpx.TimeoutException as e:
            res.error = f"timeout:{type(e).__name__}"
        except httpx.TransportError as e:
            res.error = f"network:{type(e).__name__}"
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, OSError) as e:
            res.error = f"error:{type(e).__name__}"
        if res.error and tmp:
            tmp.unlink(missing_ok=True)
            res.path = None
        return None

    def _render_once(self, url: str, res: FetchResult) -> float | None:
        res.status, res.error, res.sha256, res.size = 0, "", "", 0
        res.discard()
        try:
            status, final, headers, body = self.renderers.get().render(url, self.s.timeout, self.s.render_settle_ms)
        except Exception as e:
            msg = str(e)
            res.error = "render:download" if "Download is starting" in msg else f"render:{type(e).__name__}"
            if "Target" in msg and "closed" in msg:
                self.renderers.close_current()  # crashed browser: start a fresh one next time
            return None
        res.status, res.final_url, res.headers = status, final, headers
        if status != 200:
            return None
        fd = tempfile.NamedTemporaryFile(dir=self.tmp_dir, delete=False, suffix=".part")
        with fd:
            fd.write(body)
        res.path, res.size, res.sha256 = Path(fd.name), len(body), hashlib.sha256(body).hexdigest()
        return None

    # -- convenience for discovery adapters -------------------------------
    def get(self, url: str, **kw) -> tuple[int, bytes, dict[str, str], str]:
        """GET into memory: (status, body, headers, error). Body is capped at 30 MB."""
        kw.setdefault("max_bytes", 30 * 1024 * 1024)
        res = self.fetch(url, **kw)
        body = res.read() if res.path else b""
        return res.status, body, res.headers, res.error


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(value)
            return max(0.0, dt.timestamp() - time.time())
        except (TypeError, ValueError):
            return None
