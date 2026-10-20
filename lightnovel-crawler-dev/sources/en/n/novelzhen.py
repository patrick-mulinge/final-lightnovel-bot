"""Crawler for novelzhen.com."""

import re
from typing import Optional

from lncrawl.core import Chapter, Novel, PageSoup, SoupTemplate, Volume


class NovelZhenCrawler(SoupTemplate):
    """Crawl novels published on NovelZhen."""

    base_url = ["https://novelzhen.com/"]
    language = "en"
    has_mtl = False
    has_manga = False
    can_search = False

    novel_title_selector = "h1"
    chapter_list_selector = 'a[href*="-chapter-"]'
    chapter_body_selector = "div.chapter-content"

    def _chapter_number(self, href: str) -> Optional[int]:
        match = re.search(r"-chapter-(\d+)(?:/)?$", href or "")
        return int(match.group(1)) if match else None

    def parse_chapter_url(self, soup: PageSoup, chapter: Chapter) -> None:
        chapter.url = self.absolute_url(soup.get("href", ""))

    def parse_chapter_body(self, soup: PageSoup, chapter: Chapter) -> None:
        chapter.body = self.cleaner.extract_contents(soup)

    def parse_toc(self, soup: PageSoup, novel: Novel) -> None:
        """Expand NovelZhen's paged 50-chapter catalog into individual chapters."""
        links: dict[int, tuple[str, str]] = {}

        for anchor in soup.select(self.chapter_list_selector):
            href = anchor.get("href", "")
            number = self._chapter_number(href)
            if number is None:
                continue

            text = anchor.get_text(" ", strip=True)
            title_match = re.search(
                rf"(?:#?\s*)?{number}\s*\.??\s*Chapter\s*{number}(?:\s*:\s*(.*?))?(?:\s+\d+\s+months?\s+ago)?$",
                text,
                re.IGNORECASE,
            )
            title = (
                f"Chapter {number}: {title_match.group(1).strip()}"
                if title_match and title_match.group(1)
                else f"Chapter {number}"
            )
            links[number] = (title, self.absolute_url(href))

        if not links:
            novel.volumes = [Volume(id=1)]
            novel.chapters = []
            return

        max_number = max(links)
        sample_url = next(iter(links.values()))[1]
        url_template = re.sub(r"-chapter-\d+/?$", "-chapter-{number}", sample_url)

        novel.volumes = [Volume(id=1)]
        novel.chapters = [
            Chapter(
                id=number,
                volume=1,
                title=links.get(number, (f"Chapter {number}", ""))[0],
                url=links.get(number, ("", url_template.format(number=number)))[1],
            )
            for number in range(1, max_number + 1)
        ]
