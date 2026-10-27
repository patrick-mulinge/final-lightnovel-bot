# -*- coding: utf-8 -*-
"""Crawler for Scribble Hub series pages."""

import logging
import math
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from lncrawl.core import Chapter, LegacyCrawler, Volume

logger = logging.getLogger(__name__)


class ScribbleHubCrawler(LegacyCrawler):
    base_url = [
        "https://www.scribblehub.com/",
    ]

    def _page_url(self, page):
        parsed = urlparse(self.novel_url)
        query = parse_qs(parsed.query)
        query["toc"] = [str(page)]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)

        title = soup.select_one('.fic_title[property="name"], .fic_title')
        if not title:
            raise RuntimeError("No novel title found")
        self.novel_title = title.get_text(" ", strip=True)

        author = soup.select_one('.author_bg a, .fic_author a, .auth_name_fic')
        if author:
            self.novel_author = author.get_text(" ", strip=True)

        synopsis = soup.select_one('.wi_fic_desc[property="description"]')
        if synopsis:
            self.novel_synopsis = self.cleaner.extract_contents(synopsis)

        cover = soup.select_one('.fic_image img[property="image"], .fic_image img')
        if cover and cover.get("src"):
            self.novel_cover = self.absolute_url(cover["src"])

        count_tag = soup.select_one("#chpcounter, .cnt_toc")
        try:
            total = int(count_tag.get("value") or count_tag.get_text(strip=True))
        except (AttributeError, TypeError, ValueError):
            total = 15
        pages = max(1, math.ceil(total / 15))

        volume = Volume(id=1, title="")
        self.volumes.append(volume)
        chapter_items = []
        seen = set()
        for page in range(1, pages + 1):
            page_soup = soup if page == 1 else self.get_soup(self._page_url(page))
            for link in page_soup.select(".toc_ol a.toc_a[href]"):
                href = link.get("href")
                if not href or href in seen:
                    continue
                seen.add(href)
                chapter_items.append(
                    (link.get_text(" ", strip=True), self.absolute_url(href))
                )

        # ScribbleHub lists newest chapters first. Assign IDs after reversing
        # so the EPUB is ordered from the oldest chapter to the newest.
        for chapter_id, (title_text, chapter_url) in enumerate(
            reversed(chapter_items), start=1
        ):
            self.chapters.append(
                Chapter(
                    id=chapter_id,
                    volume=volume.id,
                    title=title_text,
                    url=chapter_url,
                )
            )
    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter["url"])
        content = soup.select_one("#chp_raw")
        if not content:
            return ""
        for node in content.select("script, style, img"):
            node.decompose()
        return self.cleaner.extract_contents(content)

