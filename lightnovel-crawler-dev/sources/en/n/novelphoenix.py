# -*- coding: utf-8 -*-
import logging
import re
from typing import Iterable, Optional
from urllib.parse import parse_qs, urlsplit

from lncrawl.core import Chapter, Novel, PageSoup, SoupTemplate, Volume

logger = logging.getLogger(__name__)

# Each row ends with how long ago the chapter went up.
POSTED_AGO = re.compile(r"\s*\d+\s+(?:second|minute|hour|day|week|month|year)s?\s+ago\s*$", re.I)
SPACES = re.compile(r"\s+")


class NovelPhoenixCrawler(SoupTemplate):
    base_url = ["https://novelphoenix.com/"]

    novel_title_selector = "h1"
    novel_synopsis_selector = "div.summary .content, div.description"

    # `article` and `main` also hold the text, but each wraps it in the post header.
    chapter_body_selector = "#content"

    def select_chapter_tags(
        self, tag: PageSoup, novel: Novel, volume: Optional[Volume] = None
    ) -> Iterable[PageSoup]:
        # The novel page links only the newest chapter; the list lives one level down.
        url = self.absolute_url(novel.url).split("?")[0].rstrip("/") + "/chapters"
        seen = set()
        while url and url not in seen:
            seen.add(url)
            soup = self.scraper.get_soup(url)
            yield from soup.select("ul.chapter-list li a[href]")

            # The site does not consistently mark the next button with rel="next".
            # Instead, follow the smallest pagination number greater than the page
            # currently being fetched. This works even when the arrow text is hidden.
            current_page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
            candidates = []
            page_controls = list(soup.select("a[href*='chapters?page=']"))
            page_controls.extend(soup.select("option[value*='chapters?page=']"))
            for link in page_controls:
                href = link.get("href") or link.get("value")
                if not href:
                    continue
                page_values = parse_qs(urlsplit(href).query).get("page", [])
                if not page_values:
                    continue
                try:
                    page_number = int(page_values[0])
                except ValueError:
                    continue
                if page_number > current_page:
                    candidates.append((page_number, href.split("#", 1)[0]))

            if not candidates:
                break
            url = self.absolute_url(min(candidates)[1])

    def parse_chapter_title(self, soup: PageSoup, chapter: Chapter) -> None:
        # A row runs number, title and timestamp together with no separators between the
        # spans, so the text has to be read with one inserted.
        text = SPACES.sub(" ", soup.get_text(" ", strip=True))
        chapter.title = POSTED_AGO.sub("", text).strip()
