# -*- coding: utf-8 -*-
"""Crawler for the Hako/DocLN Vietnamese novel sites."""

import logging
import re
import time
from urllib.parse import quote_plus

from lncrawl.core import Chapter, LegacyCrawler, Volume

logger = logging.getLogger(__name__)


class ListNovelCrawler(LegacyCrawler):
    has_mtl = True
    base_url = [
        "https://ln.hako.vn/",
        "https://docln.net/",
    ]

    def search_novel(self, query):
        search_url = "%s/tim-kiem-nang-cao?title=%s"
        soup = self.get_soup(
            search_url % (self.scraper.origin.strip("/"), quote_plus(query.lower()))
        )
        results = []
        for tab in soup.select(".sect-body .thumb-item-flow"):
            link = tab.select_one(".series-title a")
            if not link:
                continue
            latest_volume = tab.select_one(".volume-title")
            latest_chapter = tab.select_one(".chapter-title a")
            results.append(
                {
                    "title": link.get("title") or link.get_text(" ", strip=True),
                    "url": self.absolute_url(link.get("href", "")),
                    "info": " | ".join(
                        value
                        for value in (
                            latest_volume.get_text(" ", strip=True)
                            if latest_volume
                            else "",
                            latest_chapter.get("title", "") if latest_chapter else "",
                        )
                        if value
                    ),
                }
            )
        return results

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        title = soup.select_one(".series-name") or soup.select_one("h1")
        if not title:
            raise RuntimeError("No novel title")
        self.novel_title = title.get_text(" ", strip=True)

        self.novel_author = " ".join(
            author.get_text(" ", strip=True)
            for author in soup.select('.info-value a[href*="/tac-gia/"]')
        )

        cover = soup.select_one(".series-cover .img-in-ratio")
        if cover:
            match = re.search(r"url\(['\"]?([^)'\"]+)", cover.get("style", ""))
            if match:
                self.novel_cover = self.absolute_url(match.group(1))

        for section in soup.select(".volume-list"):
            section_title = section.select_one(".sect-title")
            volume = Volume(
                id=len(self.volumes) + 1,
                title=section_title.get_text(" ", strip=True) if section_title else "",
            )
            self.volumes.append(volume)

            # Exclude illustrations and side stories; they do not use -chuong-.
            for link in section.select('a[href*="-chuong-"]'):
                href = link.get("href", "")
                if not href:
                    continue
                self.chapters.append(
                    Chapter(
                        id=len(self.chapters) + 1,
                        volume=volume.id,
                        title=link.get("title") or link.get_text(" ", strip=True),
                        url=self.absolute_url(href),
                    )
                )

    def download_chapter_body(self, chapter):
        # Hako sends a hidden title placeholder before the browser verification
        # finishes. Wait for a visible chapter paragraph instead.
        soup = self.render_soup(
            chapter.url,
            wait_for='#chapter-content p[id]',
            timeout=120,
        )
        chapter_content = soup.select_one("#chapter-content") if soup else None
        if not chapter_content:
            return ""

        paragraphs = chapter_content.select(
            '#chapter-content p[id]'
        )
        body = "".join(str(paragraph) for paragraph in paragraphs)
        if body:
            time.sleep(10)
        return body










