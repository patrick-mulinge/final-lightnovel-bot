# -*- coding: utf-8 -*-
"""Crawler for LibRead, whose chapter pages redirect to FreeWebNovel."""

import re

from lncrawl.core import Chapter, LegacyCrawler, Volume


class LibreadComCrawler(LegacyCrawler):
    base_url = ["https://libread.com/"]

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)

        title = soup.select_one('meta[property="og:novel:novel_name"]')
        if title:
            self.novel_title = title.get("content", "").strip()
        if not self.novel_title:
            heading = soup.select_one("h1, h3.title")
            self.novel_title = heading.get_text(" ", strip=True) if heading else ""

        author = soup.select_one('meta[property="og:novel:author"]')
        if author:
            self.novel_author = author.get("content", "").strip()

        image = soup.select_one('meta[property="og:image"], meta[name="image"]')
        if image:
            self.novel_cover = self.absolute_url(image.get("content", ""))

        description = soup.select_one('meta[property="og:description"], meta[name="description"]')
        if description:
            self.novel_synopsis = description.get("content", "").strip()

        scripts = " ".join(script.get_text(" ") for script in soup.select("script"))
        match = re.search(r"totalChapters\s*:\s*(\d+)", scripts)
        if not match:
            match = re.search(r"lastest_chapter_url[^>]+chapter-(\d+)", str(soup))
        total = int(match.group(1)) if match else 0
        if total <= 0:
            raise RuntimeError("LibRead chapter count was not found")

        self.volumes.append(Volume(id=1))
        base = self.novel_url.rstrip("/")
        for number in range(1, total + 1):
            # LibRead uses zero-padding for early chapters; its redirect accepts
            # the four-digit form and sends the request to FreeWebNovel.
            chapter_path = f"chapter-{number:04d}"
            self.chapters.append(
                Chapter(
                    id=number,
                    volume=1,
                    title=f"Chapter {number}",
                    url=f"{base}/{chapter_path}",
                )
            )

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter.url)
        content = soup.select_one("#chr-content, #chapter-content, .m-read .txt")
        if not content:
            return ""
        for image in content.select("img"):
            image.decompose()
        return self.cleaner.extract_contents(content)




