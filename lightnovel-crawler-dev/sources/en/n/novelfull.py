# -*- coding: utf-8 -*-
"""Crawler for novelfull.com and novelfull.net."""

from urllib.parse import quote_plus

from lncrawl.core import SearchResult
from lncrawl.templates.novelfull import NovelFullTemplate


class NovelFullCrawler(NovelFullTemplate):
    can_search = True
    base_url = [
        "http://novelfull.com/",
        "https://novelfull.com/",
        "https://novelfull.net/",
    ]

    def initialize(self):
        self.cleaner.bad_css.update(
            ['div[align="left"]', 'img[src*="proxy?container=focus"]']
        )

    def search_novel(self, query):
        url = f"{self.scraper.origin.rstrip('/')}/search?keyword={quote_plus(query)}"
        soup = self.get_soup(url)
        seen = set()
        for link in soup.select('.fwn-rank-list .li-row h3.tit a[href]'):
            href = link.get("href", "").split("#", 1)[0]
            title = link.get("title") or link.get_text(" ", strip=True)
            if not href or not title or href in seen:
                continue
            seen.add(href)
            yield SearchResult(title=title, url=self.absolute_url(href))
