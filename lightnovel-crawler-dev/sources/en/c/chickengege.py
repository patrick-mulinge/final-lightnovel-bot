# -*- coding: utf-8 -*-
import logging

from lncrawl.core import SearchResult, Chapter, PageSoup, SoupTemplate

logger = logging.getLogger(__name__)


class ChickenGegeCrawler(SoupTemplate):
    def search_novel(self, query):
        soup = self.get_soup(f"{self.scraper.origin.rstrip('/')}/?s={quote(query)}&post_type=any")
        seen = set()
        for link in soup.select("a[href]"):
            href = link.get("href", "").split("#", 1)[0]
            title = link.get_text(" ", strip=True)
            if href and title and href not in seen and any(x in href for x in ("/novel/", "/book/", "/story/")):
                seen.add(href)
                yield SearchResult(title=title, url=self.absolute_url(href))
    can_search = True
    base_url = ["https://www.chickengege.org/"]

    novel_title_selector = "h1.entry-title"
    novel_cover_selector = "img.novelist-cover-image"
    chapter_list_selector = "ul#novelList a, ul#extraList a, table#novelList a"
    chapter_body_selector = "article div.entry-content"

    def initialize(self) -> None:
        self.cleaner.bad_css.update([".m-a-box", ".m-a-box-container"])

    def parse_chapter_body(self, soup: PageSoup, chapter: Chapter) -> None:
        self.cleaner.clean_contents(soup)
        chapter.body = str(soup)
