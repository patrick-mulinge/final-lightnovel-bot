# -*- coding: utf-8 -*-
"""Crawler for mtl-novel.com."""

import logging

from lncrawl.core import Chapter, LegacyCrawler, Volume

logger = logging.getLogger(__name__)


class MtlNovelCrawler(LegacyCrawler):
    base_url = [
        "https://mtl-novel.com/",
    ]
    has_mtl = True

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)

        title = soup.select_one("h1.entry-title")
        if not title:
            title = soup.select_one('meta[property="og:title"]')
        if not title:
            raise RuntimeError("No novel title found")
        self.novel_title = (
            title.get("content", "").strip()
            if title.name == "meta"
            else title.get_text(" ", strip=True)
        )

        summary = soup.select_one(".desc")
        if summary:
            self.novel_synopsis = self.cleaner.extract_contents(summary)

        author = soup.select_one("table.info tr")
        for row in soup.select("table.info tr"):
            label = row.select_one(".tbl-lbl")
            if label and label.get_text(" ", strip=True).lower() == "author":
                self.novel_author = row.get_text(" ", strip=True)
                self.novel_author = self.novel_author.split("Author", 1)[-1].strip()
                break

        cover = soup.select_one(".nov-head img")
        if cover:
            self.novel_cover = self.absolute_url(
                cover.get("data-src") or cover.get("src", "")
            )

        volume = Volume(id=1, title="")
        self.volumes.append(volume)
        for link in soup.select(".ch-list a.ch-link[href]"):
            strong = link.select_one("strong")
            title_text = link.get_text(" ", strip=True)
            if strong:
                number = strong.get_text(" ", strip=True)
                remainder = title_text[len(number):].strip()
                title_text = f"{number} {remainder}".strip()
            self.chapters.append(
                Chapter(
                    id=len(self.chapters) + 1,
                    volume=volume.id,
                    title=title_text,
                    url=self.absolute_url(link["href"]),
                )
            )

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter["url"])
        content = soup.select_one("#chapter-par")
        return self.cleaner.extract_contents(content) if content else ""
