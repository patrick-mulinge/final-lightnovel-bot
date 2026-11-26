# -*- coding: utf-8 -*-
"""Crawler for Novelib stories and chapters."""

import re
from urllib.parse import urljoin

from lncrawl.core import Chapter, LegacyCrawler, SearchResult, Volume


class NovelibCrawler(LegacyCrawler):
    base_url = ["https://novelib.com/"]
    can_search = True

    def search_novel(self, query: str):
        soup = self.get_soup(self.scraper.origin)
        scripts = " ".join(script.get_text(" ") for script in soup.select("script"))
        nonce_match = re.search(
            r"action\s*:\s*['\"]fictioneer_unrestricted_search['\"].{0,300}?"
            r"nonce\s*:\s*['\"]([^'\"]+)",
            scripts,
            re.DOTALL,
        )
        if not nonce_match:
            return

        response = self.submit_form(
            f"{self.scraper.origin.rstrip('/')}/wp-admin/admin-ajax.php",
            data={
                "action": "fictioneer_unrestricted_search",
                "search_term": query,
                "nonce": nonce_match.group(1),
            },
        )
        for item in response.json().get("data", []):
            title = item.get("title")
            href = item.get("link")
            if title and href:
                yield SearchResult(
                    title=title,
                    url=urljoin(self.scraper.origin, href),
                    info=item.get("genres", ""),
                )

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
        self.novel_synopsis = self.cleaner.extract_contents(synopsis) if synopsis else ""

        image = soup.select_one("meta[property='og:image']")
        if image:
            self.novel_cover = urljoin(self.scraper.origin, image.get("content", ""))
        if not self.novel_cover:
            image = soup.select_one(
                "img.story__thumbnail-image, .story__cover img, .story__thumbnail img"
            )
            if image:
                self.novel_cover = urljoin(self.scraper.origin, image.get("src", ""))

        chapters_tab = soup.select_one('[data-tab-name="chapters"]') or soup.select_one(
            "#tab-chapters"
        )
        if not chapters_tab:
            raise RuntimeError("Novelib chapter catalogue was empty")

        self.volumes.append(Volume(id=1, title="Volume 1"))
        seen = set()
        for link in chapters_tab.select("a.chapter-group__list-item-link[href]"):
            href = link.get("href", "").split("#", 1)[0].strip()
            if not href or href in seen:
                continue
            seen.add(href)
            title = link.get_text(" ", strip=True)
            self.chapters.append(
                Chapter(
                    id=len(self.chapters) + 1,
                    volume=1,
                    title=title or f"Chapter {len(self.chapters) + 1}",
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
