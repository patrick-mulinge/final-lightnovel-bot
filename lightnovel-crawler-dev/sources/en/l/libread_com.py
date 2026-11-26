# -*- coding: utf-8 -*-
"""Crawler for LibRead, whose chapter pages redirect to FreeWebNovel."""

import re

from bs4 import BeautifulSoup
from lncrawl.core import Chapter, LegacyCrawler, SearchResult, Volume


class LibreadComCrawler(LegacyCrawler):
    base_url = ["https://libread.com/"]
    can_search = True

    def search_novel(self, query):
        response = self.post_response(
            f"{self.scraper.origin.rstrip('/')}/search",
            data={"searchkey": query},
        )
        soup = BeautifulSoup(response.text, "html.parser")
        seen = set()
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            title = link.get_text(" ", strip=True)
            if not href or not title or href in seen or "/novel/" not in href:
                continue
            seen.add(href)
            yield SearchResult(title=title, url=self.absolute_url(href))

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        title = soup.select_one('meta[property="og:novel:novel_name"]')
        self.novel_title = title.get("content", "").strip() if title else ""
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
            self.chapters.append(Chapter(id=number, volume=1, title=f"Chapter {number}", url=f"{base}/chapter-{number:04d}"))

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter.url)
        content = soup.select_one("#chr-content, #chapter-content, .m-read .txt")
        if not content:
            return ""
        for image in content.select("img"):
            image.decompose()
        return self.cleaner.extract_contents(content)
