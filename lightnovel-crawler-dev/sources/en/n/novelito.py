# -*- coding: utf-8 -*-
"""Crawler for Novelito public novel and chapter APIs."""

from html import escape
from urllib.parse import urlparse

from lncrawl.core import Chapter, LegacyCrawler, Volume


class NovelitoCrawler(LegacyCrawler):
    base_url = ["https://noveli.to/"]
    browser_user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36"
    )

    def api_headers(self, referer):
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
            "User-Agent": self.browser_user_agent,
        }

    def read_novel_info(self):
        slug = urlparse(self.novel_url).path.rstrip("/").split("/")[-1]
        response = self.get_response(
            f"{self.scraper.origin}api/novels/{slug}",
            headers=self.api_headers(self.novel_url),
        ).json()
        novel = response.get("data", {})
        if not response.get("success") or not novel:
            raise RuntimeError("Novelito novel API returned no data")

        novel_id = novel["id"]
        self.novel_title = novel.get("title", "")
        self.novel_author = novel.get("author", "")
        self.novel_synopsis = novel.get("description", "")
        cover = novel.get("cover_image", "")
        if cover:
            self.novel_cover = f"{self.scraper.origin.rstrip('/')}/{cover.lstrip('/')}"

        self.volumes.append(Volume(id=1, title="Volume 1"))
        catalogue = self.get_response(
            f"{self.scraper.origin}api/novels/{novel_id}/chapters",
            headers=self.api_headers(self.novel_url),
        ).json()
        for item in catalogue.get("chapters", []):
            chapter_id = item.get("id")
            number = item.get("chapter_number")
            if chapter_id is None or number is None:
                continue
            self.chapters.append(Chapter(
                id=int(number),
                volume=1,
                title=item.get("title") or f"Chapter {number}",
                url=f"{self.scraper.origin}read/{novel_id}/{chapter_id}",
            ))
        self.chapters.sort(key=lambda chapter: chapter.id)
        if not self.chapters:
            raise RuntimeError("Novelito chapter catalogue was empty")

    def download_chapter_body(self, chapter):
        path_parts = urlparse(chapter.url).path.rstrip("/").split("/")
        novel_id, chapter_id = path_parts[-2:]
        response = self.get_response(
            f"{self.scraper.origin}api/novels/{novel_id}/chapters/{chapter_id}",
            headers=self.api_headers(chapter.url),
        ).json()
        chapter_data = response.get("chapter", {})
        paragraphs = []
        for paragraph in chapter_data.get("paragraphs", []):
            text = paragraph.get("text", "").strip()
            if (
                not text
                or paragraph.get("isHoneypot")
                or paragraph.get("isMidWatermark")
                or "Provided by Noveli.to" in text
                or "read on Noveli.to" in text
                or "support more free releases" in text
            ):
                continue
            paragraphs.append(f"<p>{escape(text)}</p>")
        return "\n".join(paragraphs)

