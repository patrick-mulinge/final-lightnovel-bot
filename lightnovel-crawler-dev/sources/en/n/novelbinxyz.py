# -*- coding: utf-8 -*-
"""Crawler for Novelbin.xyz."""

import base64
import json
from urllib.parse import urlparse

from lncrawl.core import Chapter, LegacyCrawler, SearchResult, Volume

class NovelbinxyzCrawler(LegacyCrawler):
    base_url = ["https://novelbin.xyz/"]
    cdn_url = "https://boxnovela.b-cdn.net"
    can_search = True

    def search_novel(self, query):
        response = self.get_response(
            f"{self.cdn_url}/lists/web_search.json",
            headers=self._headers(),
        )
        try:
            items = json.loads(base64.b64decode(response.text).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            items = []
        query = query.casefold()
        for item in items:
            title = item.get("title", "")
            alternatives = item.get("alternative_titles", "")
            if query not in f"{title} {alternatives}".casefold():
                continue
            slug = item.get("slug")
            if slug:
                yield SearchResult(title=title, url=f"{self.scraper.origin.rstrip('/')}/novel/{slug}")

    def _headers(self):
        return {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://novelbin.xyz",
            "Referer": getattr(self, "novel_url", self.base_url[0]),
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/141.0.0.0 Safari/537.36"
            ),
        }

    def read_novel_info(self):
        slug = urlparse(self.novel_url).path.rstrip("/").split("/")[-1]
        metadata = self.get_response(
            f"{self.cdn_url}/novel/{slug}.json",
            headers=self._headers(),
        ).json()

        novel_id = metadata.get("id")
        if not novel_id:
            raise RuntimeError("Novelbin.xyz novel CDN ID was not found")

        self.novel_title = metadata.get("title", "")
        self.novel_author = metadata.get("author", "")
        self.novel_synopsis = metadata.get("description", "")
        self.novel_cover = metadata.get("cover", "")

        chapter_items = self.get_response(
            f"{self.cdn_url}/chapters/{novel_id}.json",
            headers=self._headers(),
        ).json()
        if not isinstance(chapter_items, list) or not chapter_items:
            raise RuntimeError("Novelbin.xyz chapter catalogue was empty")

        self.volumes.append(Volume(id=1, title="Volume 1"))
        for position, item in enumerate(chapter_items, start=1):
            chapter_index = item.get("tocIndex", item.get("id", position - 1))
            self.chapters.append(
                Chapter(
                    id=position,
                    volume=1,
                    title=item.get("title") or f"Chapter {position}",
                    url=f"{self.cdn_url}/read/{novel_id}/{chapter_index}.json",
                )
            )

    def download_chapter_body(self, chapter):
        data = self.get_response(chapter.url, headers=self._headers()).json()
        return data.get("content", "")