"""robots.txt parsing (RFC 9309): wildcards, longest match, Crawl-delay, Sitemap, Content-Signal."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)
    return re.compile(rx + ("$" if anchored else ""))


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str, re.Pattern[str]]] = field(default_factory=list)  # allow, raw, rx
    crawl_delay: float | None = None
    content_signals: dict[str, str] = field(default_factory=dict)


@dataclass
class Robots:
    """Parsed robots.txt for one origin. `status` records how it was obtained."""

    status: str = "ok"  # ok | missing (4xx: everything allowed) | unreachable (5xx: nothing allowed)
    groups: list[Group] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> "Robots":
        rb = cls()
        cur: Group | None = None
        last_was_agent = False
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (p.strip() for p in line.split(":", 1))
            key = key.lower()
            if key == "sitemap":
                if value:
                    rb.sitemaps.append(value)
                continue
            if key == "user-agent":
                if not last_was_agent:
                    cur = Group()
                    rb.groups.append(cur)
                cur.agents.append(value.lower())  # type: ignore[union-attr]
                last_was_agent = True
                continue
            last_was_agent = False
            if cur is None:
                continue
            if key in ("allow", "disallow"):
                if value:
                    cur.rules.append((key == "allow", value, _compile(value)))
            elif key == "crawl-delay":
                try:
                    cur.crawl_delay = float(value)
                except ValueError:
                    pass
            elif key == "content-signal":
                for part in value.split(","):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cur.content_signals[k.strip().lower()] = v.strip().lower()
        return rb

    def group_for(self, agent: str) -> Group | None:
        """Most specific group naming our product token, else the `*` group."""
        token = agent.split("/", 1)[0].strip().lower()
        best: tuple[int, Group] | None = None
        star: Group | None = None
        for g in self.groups:
            for a in g.agents:
                if a == "*":
                    star = star or g
                elif a and a in token and (best is None or len(a) > best[0]):
                    best = (len(a), g)
        return best[1] if best else star

    def allowed(self, url: str, agent: str) -> bool:
        if self.status == "unreachable":
            return False
        if self.status == "missing":
            return True
        g = self.group_for(agent)
        if g is None:
            return True
        if g.content_signals.get("search") == "no":
            return False
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if target.startswith("/robots.txt"):
            return True
        verdict, length = True, -1
        for allow, raw, rx in g.rules:
            if rx.match(target) and (len(raw) > length or (len(raw) == length and allow)):
                verdict, length = allow, len(raw)
        return verdict

    def crawl_delay(self, agent: str) -> float | None:
        g = self.group_for(agent)
        return g.crawl_delay if g else None

    def signals(self, agent: str) -> dict[str, str]:
        g = self.group_for(agent)
        return dict(g.content_signals) if g else {}

    def summary(self, agent: str) -> str:
        if self.status != "ok":
            return self.status
        g = self.group_for(agent)
        if g is None:
            return "ok: no matching group (all allowed)"
        dis = [raw for allow, raw, _ in g.rules if not allow]
        if "/" in dis:
            return "ok: fully disallowed for this agent"
        bits = [f"{len(dis)} disallow rule(s)" if dis else "all allowed"]
        if g.crawl_delay:
            bits.append(f"crawl-delay {g.crawl_delay:g}s")
        if g.content_signals:
            bits.append("content-signal " + ",".join(f"{k}={v}" for k, v in g.content_signals.items()))
        return "ok: " + "; ".join(bits)
