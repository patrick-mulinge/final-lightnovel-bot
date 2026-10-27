# -*- coding: utf-8 -*-
"""Crawler for NovelNow stories."""

import re
from urllib.parse import urljoin

from lncrawl.core import Chapter, LegacyCrawler, Volume


class NovelNowCrawler(LegacyCrawler):
    base_url = ["https://www.novelnow.com/"]

    def _book_id(self):
        match = re.search(r"_([a-f0-9]{20,})/?$", self.novel_url, re.I)
        if not match:
            raise RuntimeError("NovelNow book ID was not found in the URL")
        return match.group(1)

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        book_id = self._book_id()

        data = self.get_json(f"https://api.lyramob.com/v1/detail/{book_id}")
        detail = data.get("data", data)

        title = soup.select_one(".book-detail-header h3")
        self.novel_title = (
            title.get_text(" ", strip=True)
            if title
            else detail.get("name", "") or detail.get("title", "")
        )
        self.novel_author = detail.get("author", "")

        synopsis = soup.select_one(".book-desc")
        if synopsis:
            heading = synopsis.select_one("h4")
            if heading:
                heading.decompose()
            self.novel_synopsis = synopsis.get_text(" ", strip=True)
        else:
            self.novel_synopsis = detail.get("description", "")

        cover = detail.get("cover", "")
        if cover:
            self.novel_cover = urljoin("https://s.lyramob.com/images/", cover)

        chapters_data = self.get_json(f"https://api.lyramob.com/v1/chapters/{book_id}")
        chapters = chapters_data.get("data", {}).get("chapters", [])
        if not chapters:
            raise RuntimeError("NovelNow chapter catalogue was empty")

        self.volumes.append(Volume(id=1))
        story_slug = self.novel_url.rstrip("/").rsplit("/", 1)[-1]
        for item in chapters:
            number_match = re.search(r"\d+", item.get("name", ""))
            number = number_match.group(0) if number_match else str(len(self.chapters) + 1)
            chapter_url = urljoin(
                self.scraper.origin,
                f"/story/{story_slug}/CHAPTER-{number}_{item['id']}",
            )
            self.chapters.append(
                Chapter(
                    id=len(self.chapters) + 1,
                    volume=1,
                    title=item.get("name", f"CHAPTER {number}"),
                    url=chapter_url,
                )
            )

    def download_chapter_body(self, chapter):
        soup = self.render_soup(
            chapter.url,
            wait_for=".read-chapter-content .inset-content p",
            timeout=120,
        )
        content = soup.select_one(".read-chapter-content .inset-content")
        if not content:
            # Retry once because NovelNow may initially return its JS shell.
            soup = self.render_soup(
                chapter.url,
                wait_for=".read-chapter-content .inset-content p",
                timeout=120,
            )
            content = soup.select_one(".read-chapter-content .inset-content")
        if not content:
            return ""
        for tag in content.select("script, style, nav, button, form, img"):
            tag.decompose()
        return self.cleaner.extract_contents(content)




