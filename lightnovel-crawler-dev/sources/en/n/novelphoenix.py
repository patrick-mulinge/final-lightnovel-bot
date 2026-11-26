# -*- coding: utf-8 -*-
"""Crawler for novelphoenix.com."""

import re
from typing import Iterable, Optional
from urllib.parse import parse_qs, quote_plus, urlsplit

from lncrawl.core import Chapter, Novel, PageSoup, SearchResult, SoupTemplate, Volume


class NovelPhoenixCrawler(SoupTemplate):
    base_url = ["https://novelphoenix.com/"]
    can_search = True
    novel_title_selector = "h1"
    novel_synopsis_selector = "div.summary .content, div.description"
    chapter_body_selector = "#content"

    def search(self, query):
        data = self.get_json(f"{self.scraper.origin.rstrip('/')}/ajax/searchLive?keyword={quote_plus(query)}&type=title")
        for item in data.get("data", []):
            title, slug = item.get("title"), item.get("slug")
            if title and slug:
                yield SearchResult(title=title, url=f"{self.scraper.origin.rstrip('/')}/novel/{slug}")

    def select_chapter_tags(self, tag: PageSoup, novel: Novel, volume: Optional[Volume] = None) -> Iterable[PageSoup]:
        url = self.absolute_url(novel.url).split("?")[0].rstrip("/") + "/chapters"
        seen = set()
        while url and url not in seen:
            seen.add(url)
            soup = self.scraper.get_soup(url)
            yield from soup.select("ul.chapter-list li a[href]")
            current = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
            choices = []
            for control in soup.select("a[href*='chapters?page='], option[value*='chapters?page=']"):
                href = control.get("href") or control.get("value")
                pages = parse_qs(urlsplit(href).query).get("page", [])
                if pages and int(pages[0]) > current:
                    choices.append((int(pages[0]), href))
            url = self.absolute_url(min(choices)[1]) if choices else None

    def parse_chapter_title(self, soup: PageSoup, chapter: Chapter):
        chapter.title = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
