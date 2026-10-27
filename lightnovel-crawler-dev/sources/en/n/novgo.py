# -*- coding: utf-8 -*-
"""Crawler for novgo.net novels."""

import json

from lncrawl.core import Chapter, LegacyCrawler, Volume


class NovgoCrawler(LegacyCrawler):
    base_url = ["https://novgo.net/"]

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)

        title = soup.select_one('meta[property="og:novel:novel_name"]')
        if title:
            self.novel_title = title.get("content", "").strip()
        if not self.novel_title:
            heading = soup.select_one("h1")
            self.novel_title = heading.get_text(" ", strip=True) if heading else ""

        author = soup.select_one('meta[property="og:novel:author"]')
        if author:
            self.novel_author = author.get("content", "").strip()

        image = soup.select_one('meta[property="og:image"]')
        if image:
            self.novel_cover = self.absolute_url(image.get("content", ""))

        summary = soup.select_one("#novel-summary-inner")
        if summary:
            self.novel_synopsis = "".join(str(p) for p in summary.select("p"))
        if not self.novel_synopsis:
            schema = soup.find("script", type="application/ld+json")
            if schema:
                try:
                    self.novel_synopsis = json.loads(schema.get_text()).get("description", "")
                except (TypeError, json.JSONDecodeError):
                    pass

        listing = soup.select_one("#list-chapter[data-novel-id]")
        if not listing:
            raise RuntimeError("NovGo novel id was not found")

        novel_id = listing.get("data-novel-id", "")
        chapter_soup = self.get_soup(
            f"{self.scraper.origin.rstrip('/')}/ajax-chapter-option?novelId={novel_id}"
        )
        chapter_links = chapter_soup.select("option[value]")
        if not chapter_links:
            raise RuntimeError("NovGo chapter list was empty")

        self.volumes.append(Volume(id=1))
        for number, link in enumerate(chapter_links, start=1):
            self.chapters.append(
                Chapter(
                    id=number,
                    volume=1,
                    title=link.get_text(" ", strip=True) or f"Chapter {number}",
                    url=self.absolute_url(link.get("value", "")),
                )
            )

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter.url)
        content = soup.select_one("#chapter-content")
        if not content:
            return ""
        for tag in content.select("script, style, .ads, .ad, [aria-hidden='true']"):
            tag.decompose()
        return self.cleaner.extract_contents(content)
