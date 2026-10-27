# -*- coding: utf-8 -*-
"""Crawler for WNTL (wntl.net)."""

from html import escape

from lncrawl.core import Chapter, LegacyCrawler, Volume


class WntlCrawler(LegacyCrawler):
    base_url = ["https://wntl.net/"]

    def _novel_id(self):
        return self.novel_url.rstrip("/").split("/")[-1]

    def read_novel_info(self):
        novel_id = self._novel_id()
        novels = self.get_json(f"{self.scraper.origin.rstrip('/')}/api/novels")
        entries = novels.get("novels", []) if isinstance(novels, dict) else []
        novel_data = next((item for item in entries if str(item.get("id")) == novel_id), None)
        if not novel_data:
            raise RuntimeError(f"WNTL novel was not found: {novel_id}")

        self.novel_title = str(novel_data.get("title", "")).strip()
        self.novel_author = str(novel_data.get("author", "")).strip()
        if novel_data.get("cover"):
            self.novel_cover = self.absolute_url(str(novel_data["cover"]))

        description = novel_data.get("description", "")
        self.novel_synopsis = "\n\n".join(map(str, description)) if isinstance(description, list) else str(description).strip()

        response = self.get_json(f"{self.scraper.origin.rstrip('/')}/api/chapters/{novel_id}")
        items = response.get("chapters", []) if isinstance(response, dict) else []
        if not items:
            raise RuntimeError("WNTL returned no chapters")

        self.volumes.append(Volume(id=1))
        for sequence, item in enumerate(items, start=1):
            chapter_file = str(item.get("file", "")).strip()
            if not chapter_file:
                continue
            title = str(item.get("title") or f"Chapter {item.get('number', sequence)}").strip()
            self.chapters.append(
                Chapter(
                    id=sequence,
                    volume=1,
                    title=title,
                    url=(
                        f"{self.scraper.origin.rstrip('/')}/api/chapter-content/"
                        f"{novel_id}/{chapter_file}"
                    ),
                )
            )

    def download_chapter_body(self, chapter):
        # WNTL's normal reader route is a JavaScript app shell. The API returns
        # the chapter's original Markdown/plain-text content directly.
        text = self.get_response(chapter.url).text.strip()
        if not text:
            return ""
        paragraphs = (part.strip() for part in text.split("\n\n"))
        return "".join(f"<p>{escape(part).replace(chr(10), '<br />')}</p>" for part in paragraphs if part)
