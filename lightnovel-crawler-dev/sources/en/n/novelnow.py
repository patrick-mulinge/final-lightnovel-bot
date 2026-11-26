# -*- coding: utf-8 -*-
"""Crawler for novelnow.com."""

import re
from urllib.parse import urljoin

from lncrawl.core import Chapter, LegacyCrawler, SearchResult, Volume


class NovelNowCrawler(LegacyCrawler):
    base_url = ["https://www.novelnow.com/"]
    can_search = True

    def search_novel(self, query):
        data = self.get_json(f"https://api.lyramob.com/v1/search?k={query}&pageNum=1&pageSize=10&risk=5&country=us&product=novelfun")
        for item in data.get("data", []):
            book_id = item.get("id")
            title = item.get("name") or item.get("title")
            if book_id and title:
                yield SearchResult(title=title, url=f"{self.scraper.origin.rstrip('/')}/book/{title.replace(' ', '-').lower()}_{book_id}", author=item.get("author", ""))

    def _book_id(self):
        match = re.search(r"_([a-f0-9]{20,})/?$", self.novel_url, re.I)
        if not match:
            raise RuntimeError("NovelNow book ID was not found in the URL")
        return match.group(1)

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        book_id = self._book_id()
        detail = self.get_json(f"https://api.lyramob.com/v1/detail/{book_id}").get("data", {})
        title = soup.select_one(".book-detail-header h3")
        self.novel_title = title.get_text(" ", strip=True) if title else detail.get("name", "")
        self.novel_author = detail.get("author", "")
        synopsis = soup.select_one(".book-desc")
        self.novel_synopsis = synopsis.get_text(" ", strip=True) if synopsis else detail.get("description", "")
        if detail.get("cover"):
            self.novel_cover = urljoin("https://s.lyramob.com/images/", detail["cover"])
        chapters = self.get_json(f"https://api.lyramob.com/v1/chapters/{book_id}").get("data", {}).get("chapters", [])
        self.volumes.append(Volume(id=1))
        slug = self.novel_url.rstrip("/").rsplit("/", 1)[-1]
        for item in chapters:
            number = re.search(r"\d+", item.get("name", ""))
            number = number.group(0) if number else str(len(self.chapters) + 1)
            self.chapters.append(Chapter(id=len(self.chapters) + 1, volume=1, title=item.get("name", f"Chapter {number}"), url=urljoin(self.scraper.origin, f"/story/{slug}/CHAPTER-{number}_{item['id']}")))

    def download_chapter_body(self, chapter):
        soup = self.render_soup(chapter.url, wait_for=".read-chapter-content .inset-content p", timeout=120)
        content = soup.select_one(".read-chapter-content .inset-content")
        if not content:
            return ""
        for tag in content.select("script, style, nav, button, form, img"):
            tag.decompose()
        return self.cleaner.extract_contents(content)
