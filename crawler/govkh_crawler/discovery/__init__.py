from .base import Adapter, Candidate
from .ckan import CkanAdapter
from .crawl import CrawlAdapter
from .moeys import MoeysAdapter
from .sitemap import SitemapAdapter
from .wordpress import WordPressAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    a.type: a for a in (SitemapAdapter, WordPressAdapter, CkanAdapter, CrawlAdapter, MoeysAdapter)
}

__all__ = ["ADAPTERS", "Adapter", "Candidate"]
