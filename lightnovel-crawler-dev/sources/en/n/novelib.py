# -*- coding: utf-8 -*-
"""Crawler for Novelib stories and chapters."""

import re
from urllib.parse import urljoin

from lncrawl.core import Chapter, LegacyCrawler, Volume


class NovelibCrawler(LegacyCrawler):
    base_url = ["https://novelib.com/"]

    def initialize(self):
        self.cleaner.bad_tags.update(["script", "style", "nav", "form", "button"])
        self.cleaner.bad_css.update(
            [".ads", ".ad", "[class*='advert']", ".chapter__actions", ".chapter__comments"]
        )

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)

        title = soup.select_one("h1.story__identity-title, h1.story__title, h1")
        self.novel_title = title.get_text(" ", strip=True) if title else ""

        author = soup.select_one(".author-name, .story__author, .story__author-name, a.author")
        self.novel_author = author.get_text(" ", strip=True) if author else ""

        synopsis = soup.select_one(
            ".story__description, .story__summary, .story__entry, .story__content"
        )
        self.novel_synopsis = (
            self.cleaner.extract_contents(synopsis) if synopsis else ""
        )

        image = soup.select_one("meta[property='og:image']")
        if image:
            self.novel_cover = urljoin(self.scraper.origin, image.get("content", ""))
        if not self.novel_cover:
            image = soup.select_one("img.story__thumbnail-image, .story__cover img, .story__thumbnail img")
            if image:
                self.novel_cover = urljoin(self.scraper.origin, image.get("src", ""))

        self.volumes.append(Volume(id=1, title="Volume 1"))
        seen = set()
        chapters_tab = next(
            (
                section for section in soup.find_all("section")
                if section.get("data-tab-name") == "chapters"
            ),
            soup,
        )
        for link in chapters_tab.select(
            "a.chapter-group__list-item-link[href*='/sde-ch-']"
        ):
            href = link.get("href", "").split("#", 1)[0].strip()
            if not href or href in seen:
                continue
            seen.add(href)
            text = link.get_text(" ", strip=True)
            match = re.search(r"(?:chapter|ch)\s*([0-9]+(?:-[0-9]+)?)", text, re.I)
            number = len(self.chapters) + 1
            if match:
                number = len(self.chapters) + 1
            self.chapters.append(
                Chapter(
                    id=number,
                    volume=1,
                    title=text or f"Chapter {number}",
                    url=urljoin(self.novel_url, href),
                )
            )

        if not self.chapters:
            raise RuntimeError("Novelib chapter catalogue was empty")

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter.url)
        content = soup.select_one("#chapter-content, .chapter__content")
        if not content:
            return ""
        for tag in content.select(
            "script, style, nav, form, button, .ads, .ad, .chapter__actions, .chapter__comments"
        ):
            tag.decompose()
        return self.cleaner.extract_contents(content)