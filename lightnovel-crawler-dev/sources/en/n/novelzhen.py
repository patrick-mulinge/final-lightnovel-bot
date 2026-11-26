# -*- coding: utf-8 -*-
"""Crawler for novelzhen.com."""

import re
from typing import Optional

from lncrawl.core import Chapter, Novel, PageSoup, SearchResult, SoupTemplate, Volume


class NovelZhenCrawler(SoupTemplate):
    base_url = ["https://novelzhen.com/"]
    language = "en"
    can_search = True
    novel_title_selector = "h1"
    chapter_list_selector = 'a[href*="-chapter-"]'
    chapter_body_selector = "div.chapter-content"

    def search(self, query):
        data = self.post_response(f"{self.scraper.origin.rstrip('/')}/api/searchhome", json={"query": query}).json()
        for item in data.get("results", []):
            title, slug = item.get("title"), item.get("slug")
            if title and slug:
                yield SearchResult(title=title, url=f"{self.scraper.origin.rstrip('/')}/novel/{slug}", author=item.get("author", ""))

    def _chapter_number(self, href: str) -> Optional[int]:
        match = re.search(r"-chapter-(\d+)(?:/)?$", href or "")
        return int(match.group(1)) if match else None

    def parse_chapter_url(self, soup: PageSoup, chapter: Chapter):
        chapter.url = self.absolute_url(soup.get("href", ""))

    def parse_chapter_body(self, soup: PageSoup, chapter: Chapter):
        chapter.body = self.cleaner.extract_contents(soup)

    def parse_toc(self, soup: PageSoup, novel: Novel):
        links = {}
        for anchor in soup.select(self.chapter_list_selector):
            number = self._chapter_number(anchor.get("href", ""))
            if number is not None:
                links[number] = (anchor.get_text(" ", strip=True) or f"Chapter {number}", self.absolute_url(anchor["href"]))
        novel.volumes = [Volume(id=1)]
        novel.chapters = [Chapter(id=number, volume=1, title=title, url=href) for number, (title, href) in sorted(links.items())]
