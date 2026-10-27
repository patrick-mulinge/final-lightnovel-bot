# -*- coding: utf-8 -*-
"""Crawler for Gravity Tales, including its free/premium chapter catalogue."""

import re
from urllib.parse import urljoin

from lncrawl.core import Chapter, LegacyCrawler, Volume


class GravityTalesCrawler(LegacyCrawler):
    base_url = ["https://gravitytales.com/"]

    @staticmethod
    def _meta(soup, selector):
        tag = soup.select_one(selector)
        return tag.get("content", "").strip() if tag else ""

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)

        title = soup.select_one("h1")
        self.novel_title = title.get_text(" ", strip=True) if title else self._meta(
            soup, 'meta[property="og:title"]'
        )

        summary = soup.select_one(".story__summary")
        self.novel_synopsis = summary.get_text(" ", strip=True) if summary else self._meta(
            soup, 'meta[property="og:description"]'
        )

        author = soup.select_one('.story__meta a[href*="/author/"]')
        if not author:
            author = soup.select_one('a.author[href*="/author/"]')
        self.novel_author = author.get_text(" ", strip=True) if author else ""

        cover = self._meta(soup, 'meta[property="og:image"]')
        if cover:
            self.novel_cover = urljoin(self.scraper.origin, cover)

        self.volumes.append(Volume(id=1))
        seen = set()
        for item in soup.select("li.chapter-group__list-item._publish"):
            link = item.select_one('a[href*="/story/"]')
            if not link:
                continue
            href = link.get("href", "").split("#", 1)[0].strip()
            if not href or href in seen:
                continue
            seen.add(href)
            title = link.get_text(" ", strip=True)
            match = re.search(r"\bchapter\s+(\d+(?:\.\d+)?)\b", title, re.I)
            number = float(match.group(1)) if match else len(self.chapters) + 1
            self.chapters.append(
                Chapter(
                    id=len(self.chapters) + 1,
                    volume=1,
                    title=title,
                    url=urljoin(self.scraper.origin, href),
                )
            )

        if not self.chapters:
            raise RuntimeError("Gravity Tales free chapter catalogue was empty")

        self.chapters.sort(
            key=lambda chapter: float(
                re.search(r"\bchapter\s+(\d+(?:\.\d+)?)\b", chapter.title, re.I).group(1)
            )
        )
        for chapter_id, chapter in enumerate(self.chapters, 1):
            chapter.id = chapter_id

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter.url)
        content = soup.select_one("#chapter-content")
        if not content:
            return ""
        for tag in content.select("script, style, nav, button, form, img"):
            tag.decompose()
        return self.cleaner.extract_contents(content)
