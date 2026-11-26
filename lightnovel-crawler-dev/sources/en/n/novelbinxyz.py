# -*- coding: utf-8 -*-
"""Crawler for NovelBin.xyz using its runtime CDN discovered from the site."""

import json
import re
from urllib.parse import urlparse

from lncrawl.core import Chapter, LegacyCrawler, SearchResult, Volume


class NovelbinxyzCrawler(LegacyCrawler):
    base_url = ["https://novelbin.xyz/"]
    can_search = True

    def _cdn(self):
        page = self.get_response(self.scraper.origin).text
        match = re.search(r"https?://[^\"'\s]+\.b-cdn\.net", page)
        if not match:
            for script in re.findall(r'<script[^>]+src=[\"\']([^\"\']+)', page):
                text = self.get_response(self.absolute_url(script)).text
                match = re.search(r"https?://[^\"'\s]+\.b-cdn\.net", text)
                if match:
                    break
        if not match:
            raise RuntimeError("NovelBin CDN URL was not found")
        return match.group(0).rstrip("/")

    def search_novel(self, query):
        data = self.get_response(f"{self._cdn()}/lists/web_search.json").json()
        for item in data if isinstance(data, list) else []:
            title = item.get("title", "")
            if query.casefold() in f"{title} {item.get('alternative_titles', '')}".casefold() and item.get("slug"):
                yield SearchResult(title=title, url=f"{self.scraper.origin.rstrip('/')}/novel/{item['slug']}")

    def read_novel_info(self):
        cdn = self._cdn()
        slug = urlparse(self.novel_url).path.rstrip("/").split("/")[-1]
        metadata = self.get_response(f"{cdn}/novel/{slug}.json").json()
        novel_id = metadata["id"]
        self.novel_title = metadata.get("title", "")
        self.novel_author = metadata.get("author", "")
        self.novel_synopsis = metadata.get("description", "")
        self.novel_cover = metadata.get("cover", "")
        self.volumes.append(Volume(id=1))
        for position, item in enumerate(self.get_response(f"{cdn}/chapters/{novel_id}.json").json(), 1):
            index = item.get("tocIndex", item.get("id", position - 1))
            self.chapters.append(Chapter(id=position, volume=1, title=item.get("title", f"Chapter {position}"), url=f"{cdn}/read/{novel_id}/{index}.json"))

    def download_chapter_body(self, chapter):
        return self.get_response(chapter.url).json().get("content", "")
