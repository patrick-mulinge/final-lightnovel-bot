# -*- coding: utf-8 -*-
"""Crawler for NovelCEO novel and chapter pages."""

import re
from urllib.parse import urljoin

from lncrawl.core import Chapter, LegacyCrawler, Volume


class NovelCeoCrawler(LegacyCrawler):
    base_url = ["https://novelceo.com/"]

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        title = soup.select_one("h1 a[itemprop='item'] span[itemprop='name'], h3.title")
        self.novel_title = title.get_text(" ", strip=True) if title else ""
        author = soup.select_one("[itemprop='author'] [itemprop='name'], .info a[href*='/author/']")
        self.novel_author = author.get("content", "").strip() if author and author.has_attr("content") else (author.get_text(" ", strip=True) if author else "")
        description = soup.select_one("#novel-description-content, .desc-text")
        self.novel_synopsis = description.get_text(" ", strip=True) if description else ""
        cover = soup.select_one("meta[property='og:image'], meta[itemprop='image']")
        if cover and cover.get("content"):
            self.novel_cover = urljoin(self.scraper.origin, cover["content"])

        self.volumes.append(Volume(id=1, title="Volume 1"))
        visible = {}
        for link in soup.select("a[href*='/chapter-']"):
            href = link.get("href", "")
            match = re.search(r"chapter-(\d+)", href, re.IGNORECASE)
            if not match:
                continue
            number = int(match.group(1))
            visible[number] = (
                link.get_text(" ", strip=True) or f"Chapter {number}",
                urljoin(self.scraper.origin, href),
            )

        latest = soup.select_one("meta[property='og:novel:lastest_chapter_url']")
        latest_url = latest.get("content", "") if latest else ""
        latest_match = re.search(r"chapter-(\d+)", latest_url, re.IGNORECASE)
        total = int(latest_match.group(1)) if latest_match else max(visible, default=0)
        novel_base = self.novel_url.rstrip("/")

        for number in range(1, total + 1):
            title, url = visible.get(
                number,
                (f"Chapter {number}", f"{novel_base}/chapter-{number}"),
            )
            self.chapters.append(
                Chapter(
                    id=number,
                    volume=1,
                    title=title,
                    url=url,
                )
            )
        self.chapters.sort(key=lambda chapter: chapter.id)
        if not self.chapters:
            raise RuntimeError("NovelCEO chapter catalogue was empty")

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter.url)
        content = soup.select_one("#chr-content, #chapter-content")
        if not content:
            return ""
        for tag in content.select("script, style, nav, button, img, .ads, .ad, .ad-container"):
            tag.decompose()
        return self.cleaner.extract_contents(content)










