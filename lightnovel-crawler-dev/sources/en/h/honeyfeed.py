# -*- coding: utf-8 -*-
"""Crawler for Honeyfeed."""

import re
from urllib.parse import quote_plus, urljoin

from lncrawl.core import Chapter, LegacyCrawler, SearchResult, Volume


class HoneyfeedCrawler(LegacyCrawler):
    base_url = ["https://www.honeyfeed.fm/"]
    can_search = True

    def search_novel(self, query):
        soup = self.get_soup(f"{self.scraper.origin.rstrip('/')}/search/novel_title?k={quote_plus(query)}")
        for link in soup.select('a[href*="/novels/"]'):
            title = link.get_text(" ", strip=True)
            if title:
                yield SearchResult(title=title, url=urljoin(self.scraper.origin, link["href"]))

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        meta = lambda selector: (soup.select_one(selector) or {}).get("content", "").strip()
        self.novel_title = meta('meta[property="og:title"]')
        self.novel_synopsis = meta('meta[property="og:description"]')
        self.novel_cover = self.absolute_url(meta('meta[property="og:image"]'))
        author = soup.select_one('#wrap-details a[href*="/u/"]')
        self.novel_author = author.get_text(" ", strip=True) if author else ""
        catalogue = self.get_soup(self.novel_url.rstrip("/") + "/chapters")
        self.volumes.append(Volume(id=1))
        for link in catalogue.select('a[href*="/chapters/"]'):
            match = re.search(r"/chapters/(\d+)", link.get("href", ""))
            if match:
                self.chapters.append(Chapter(id=len(self.chapters)+1, volume=1, title=link.get_text(" ", strip=True), url=urljoin(self.scraper.origin, link["href"])))

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter.url)
        content = soup.select_one("#chapter-body .wrap-body")
        if not content:
            return ""
        for tag in content.select("script, style, nav, img"):
            tag.decompose()
        return self.cleaner.extract_contents(content)
