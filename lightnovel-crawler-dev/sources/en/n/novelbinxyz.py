# -*- coding: utf-8 -*-
"""Crawler for Novelbin.xyz."""

import json
import re
from urllib.parse import urljoin

from lncrawl.core import Chapter, LegacyCrawler, Volume


class NovelbinxyzCrawler(LegacyCrawler):
    base_url = ["https://novelbin.xyz/"]
    chapter_cdn = "https://boxnovela.b-cdn.net/read/9100515"

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        self.cleaner.bad_tags.update(["script", "style", "nav", "footer", "form", "button"])
        self.cleaner.bad_css.update([".ads", ".ad", "[class*='advert']"])

        title = soup.select_one("h1.detail-title, #ssr-novel-card h1, h1")
        self.novel_title = title.get_text(" ", strip=True) if title else ""

        author = soup.select_one(".author-name, .detail-author")
        self.novel_author = author.get_text(" ", strip=True) if author else ""

        synopsis = soup.select_one(".synopsis-content, #ssr-novel-card section")
        self.novel_synopsis = self.cleaner.extract_contents(synopsis) if synopsis else ""

        cover = soup.select_one("meta[property='og:image']")
        if cover and cover.get("content"):
            self.novel_cover = urljoin(self.novel_url, cover["content"])
        else:
            image = soup.select_one("img.detail-cover, #ssr-novel-card img")
            if image and image.get("src"):
                self.novel_cover = urljoin(self.novel_url, image["src"])

        chapter_links = soup.select("a.chapter-item[href*='/read/']")
        self.volumes.append(Volume(id=1, title="Volume 1"))

        if chapter_links:
            for link in chapter_links:
                text = link.get_text(" ", strip=True)
                match = re.search(r"Chapter\s+(\d+)", text, re.IGNORECASE)
                if not match:
                    continue
                number = int(match.group(1))
                self.chapters.append(
                    Chapter(
                        id=number,
                        volume=1,
                        title=text,
                        url=urljoin(self.novel_url, link.get("href", "")),
                    )
                )
        else:
            schema = soup.select_one("script#novel-schema")
            total = 0
            if schema:
                try:
                    total = int(json.loads(schema.string or "{}").get("numberOfPages") or 0)
                except (TypeError, ValueError, AttributeError):
                    total = 0
            if not total:
                count = soup.select_one(".chapter-box-count")
                match = re.search(r"(\d[\d,]*)", count.get_text() if count else "")
                total = int(match.group(1).replace(",", "")) if match else 0
            if not total:
                raw = str(soup)
                match = re.search(r'"numberOfPages"\s*:\s*(\d+)', raw)
                if not match:
                    match = re.search(r'Total Chapters:\s*</strong>\s*(\d[\d,]*)', raw, re.IGNORECASE)
                total = int(match.group(1).replace(",", "")) if match else 0

            for number in range(1, total + 1):
                self.chapters.append(
                    Chapter(
                        id=number,
                        volume=1,
                        title=f"Chapter {number}",
                        url=f"https://novelbin.xyz/read/{self.novel_url.rstrip('/').split('/')[-1]}/chapter-{number}",
                    )
                )

        self.chapters.sort(key=lambda chapter: chapter.id)
        if not self.novel_title:
            raise RuntimeError("Novelbin.xyz novel title was empty")
        if not self.chapters:
            raise RuntimeError("Novelbin.xyz chapter catalogue was empty")

    def download_chapter_body(self, chapter):
        response = self.get_response(
            f"{self.chapter_cdn}/{chapter.id - 1}.json",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": chapter.url,
                "Origin": "https://novelbin.xyz",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/141.0.0.0 Safari/537.36"
                ),
            },
        )
        response.raise_for_status()
        data = response.json()
        return data.get("content", "")