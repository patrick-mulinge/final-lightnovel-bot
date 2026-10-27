# -*- coding: utf-8 -*-
"""Crawler for Lunox Scans novel pages."""

from typing import Iterable, Optional

from lncrawl.core import Chapter, Novel, PageSoup, SoupTemplate, Volume


class LunoxScansCrawler(SoupTemplate):
    base_url = [
        "https://lunoxscans.com/",
    ]
    has_mtl = False
    has_manga = False
    can_search = False

    novel_title_selector = ".post-title h1"
    novel_cover_selector = ".summary_image img"
    novel_author_selector = '.author-content a[href*="series-author"]'
    novel_tags_selector = '.genres-content a[rel="tag"]'
    novel_synopsis_selector = ".manga-excerpt"

    # Lunox renders all chapter links in the series page. Paid links carry
    # the premium class and are deliberately excluded.
    chapter_list_selector = "a.lunox-chapter-item.free[href]"
    chapter_title_selector = ".lunox-chapter-name"
    chapter_body_selector = ".reading-content .text-left"

    def select_chapter_tags(
        self,
        tag: PageSoup,
        novel: Novel,
        volume: Optional[Volume] = None,
    ) -> Iterable[PageSoup]:
        return reversed(tag.select(self.chapter_list_selector))

    def parse_chapter_item(self, soup: PageSoup, chapter_id: int) -> Chapter:
        chapter = Chapter(id=chapter_id)
        title = soup.select_one(self.chapter_title_selector)
        chapter.title = (title or soup).get_text(" ", strip=True)
        chapter.url = self.absolute_url(soup.get("href"))
        return chapter

