# -*- coding: utf-8 -*-
"""Crawler for mtlnovel.me."""

import logging
import re
from urllib.parse import quote_plus

from lncrawl.core import Chapter, LegacyCrawler

logger = logging.getLogger(__name__)


class MtlnovelCrawler(LegacyCrawler):
    has_mtl = True
    base_url = [
        "https://mtlnovel.me/",
    ]

    def search_novel(self, query):
        # The site search is server-rendered; keep search optional if its
        # endpoint changes, while direct novel URLs remain fully supported.
        url = f"{self.scraper.origin.rstrip('/')}/search?keyword={quote_plus(query)}"
        soup = self.get_soup(url)
        results = []
        for link in soup.select('a[href*="/info/"]'):
            href = link.get("href")
            title = link.get_text(" ", strip=True)
            if href and title:
                results.append({"url": self.absolute_url(href), "title": title})
        return results[:10]

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)

        title = soup.select_one("main h1, main h2, main h3.title, .novel-content .title")
        if not title:
            title = soup.select_one('meta[property="og:title"]')
        if not title:
            raise RuntimeError("No novel title found")
        self.novel_title = title.get("content", "").strip() if title.name == "meta" else title.get_text(" ", strip=True)
        self.novel_title = re.sub(
            r"^Read\s+|\s+(?:RAW English Translation|English MTL|MTL Novel)(?:\s*-\s*MTL Novel)?$",
            "",
            self.novel_title,
            flags=re.IGNORECASE,
        ).strip(" -")

        description = soup.select_one('meta[name="description"]')
        if description and description.get("content"):
            self.novel_synopsis = description["content"]

        image = soup.select_one('meta[property="og:image"]')
        if image and image.get("content"):
            self.novel_cover = self.absolute_url(image["content"])
        else:
            image = soup.select_one(".novel-image img")
            if image and image.get("src"):
                self.novel_cover = self.absolute_url(image["src"])

        # The live page fills the chapter list with JavaScript. Fetch the
        # same server-side fragment directly so a browser is not required.
        slug = self.novel_url.rstrip("/").split("/")[-1]
        chapter_soup = self.get_soup(
            f"{self.scraper.origin.rstrip('/')}/ajax/chapters/?slug={slug}"
        )
        chapter_links = chapter_soup.select("#list-chapters a[href]")
        if not chapter_links:
            chapter_links = chapter_soup.select("a[href*='/read/']")

        for link in chapter_links:
            href = link.get("href")
            if not href:
                continue
            title = link.get("title") or link.get_text(" ", strip=True)
            self.chapters.append(
                Chapter(
                    id=len(self.chapters) + 1,
                    title=title,
                    url=self.absolute_url(href),
                )
            )

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter["url"])
        content = soup.select_one("main article .content.text-break")
        if not content:
            content = soup.select_one("article .content")
        return self.cleaner.extract_contents(content) if content else ""




