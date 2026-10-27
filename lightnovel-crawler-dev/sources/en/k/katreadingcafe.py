# -*- coding: utf-8 -*-
"""Crawler for Kat Reading Cafe series pages."""

import logging

from lncrawl.core import Chapter, LegacyCrawler, Volume

logger = logging.getLogger(__name__)


class KatReadingCafeCrawler(LegacyCrawler):
    base_url = [
        "https://katreadingcafe.com/",
    ]
    has_mtl = False

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)

        title = soup.select_one('h1.entry-title[itemprop="name"]')
        if not title:
            title = soup.select_one("h1.entry-title")
        if not title:
            raise RuntimeError("No novel title found")
        self.novel_title = title.get_text(" ", strip=True)

        author = soup.select_one('.sertoauth .serval a, .sertoauth a[rel="author"]')
        if author:
            self.novel_author = author.get_text(" ", strip=True)

        description = soup.select_one('.sersys.entry-content[itemprop="description"]')
        if description:
            self.novel_synopsis = self.cleaner.extract_contents(description)

        cover = soup.select_one('meta[property="og:image"]')
        if cover and cover.get("content"):
            self.novel_cover = self.absolute_url(cover["content"])
        else:
            image = soup.select_one('.sertothumb img[itemprop="image"], .sertothumb img')
            if image and image.get("src"):
                self.novel_cover = self.absolute_url(image["src"])

        volume = Volume(id=1, title="")
        self.volumes.append(volume)

        chapter_items = []
        for link in soup.select('.katoc-list li a[href]'):
            item = link.find_parent("li")
            if not item or item.get("data-locked", "0") == "1":
                continue

            number = link.select_one(".epl-num")
            chapter_title = link.select_one(".epl-title")
            label = number.get_text(" ", strip=True) if number else ""
            name = chapter_title.get_text(" ", strip=True) if chapter_title else ""
            title_text = f"{label} - {name}" if name else label
            href = link.get("href")
            if not href:
                continue

            chapter_items.append(
                (title_text or link.get_text(" ", strip=True), self.absolute_url(href))
            )

        # IDs determine EPUB order, so assign them after reversing the site's
        # newest-first chapter list.
        for chapter_id, (title_text, chapter_url) in enumerate(
            reversed(chapter_items), start=1
        ):
            self.chapters.append(
                Chapter(
                    id=chapter_id,
                    volume=volume.id,
                    title=title_text,
                    url=chapter_url,
                )
            )


    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter["url"])
        body = soup.select_one('.epcontent.entry-content[itemprop="text"]')
        if not body:
            body = soup.select_one(".epcontent.entry-content")
        if not body:
            return ""

        # Kat Reading Cafe repeats the same decorative image in every chapter.
        # Remove chapter images so they are not downloaded into the EPUB.
        for node in body.select("img, .krc-wm, .ckb-announcement, script, style"):
            node.decompose()
        return self.cleaner.extract_contents(body)




