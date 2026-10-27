# -*- coding: utf-8 -*-
"""Crawler for novelroll.com."""

import json
import re

from lncrawl.core import Chapter, LegacyCrawler, Volume


class NovelrollCrawler(LegacyCrawler):
    base_url = ["https://novelroll.com/"]

    def _json_ld_book(self, soup):
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.get_text())
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and isinstance(data.get("@graph"), list):
                items = data["@graph"]
            else:
                items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "Book":
                    return item
        return {}

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        book = self._json_ld_book(soup)
        self.novel_title = str(book.get("name", "")).strip()
        self.novel_synopsis = str(book.get("description", "")).strip()
        # JSON-LD contains a shortened description ending in "...". The
        # expanded page renders the complete synopsis in this content block.
        visible_synopsis = soup.select_one(
            'div[x-ref="content"] div.text-left > div'
        )
        if visible_synopsis:
            full_synopsis = visible_synopsis.get_text(" ", strip=True)
            if full_synopsis:
                self.novel_synopsis = full_synopsis
        if book.get("image"):
            self.novel_cover = self.absolute_url(str(book["image"]))
        author = book.get("author", "")
        if isinstance(author, dict):
            author = author.get("name", "")
        self.novel_author = str(author).strip()

        # The normal page shows only a few newest and oldest links. ?toc loads
        # the complete catalogue, still ordered newest chapter first.
        toc = self.get_soup(self.novel_url.rstrip("/") + "?toc")
        book_path = self.novel_url.rstrip("/").split("novelroll.com", 1)[-1]
        if not book_path.startswith("/"):
            book_path = "/" + book_path
        book_path = book_path.rstrip("/")
        # JSON-LD includes the newest chapter even when the visible catalogue
        # has not rendered it yet. Merge both sources, retaining page order.
        entries = [
            (item.get("url", ""), item.get("name", ""))
            for item in book.get("hasPart", [])
            if isinstance(item, dict)
        ]
        entries.extend(
            (link.get("href", ""), link.get("title") or link.get_text(" ", strip=True))
            for link in toc.select('a[href*="/chapter-"]')
        )
        seen = set()
        self.volumes.append(Volume(id=1))
        for href, title in entries:
            href = href.strip()
            if not href or href in seen:
                continue
            if href.startswith("https://novelroll.com"):
                href = href.split("novelroll.com", 1)[-1]
            if not href.startswith(book_path + "/chapter-"):
                continue
            seen.add(href)
            if not title.lower().startswith("chapter"):
                continue
            self.chapters.append(
                Chapter(
                    id=len(self.chapters) + 1,
                    volume=1,
                    title=title.strip(),
                    url=self.absolute_url(href),
                )
            )
        if not self.chapters:
            raise RuntimeError("NovelRoll chapter catalogue was empty")

        # NovelRoll displays newest chapters first, but lncrawl's --first and
        # --last options are expected to operate in natural chapter order.
        def chapter_number(item):
            match = re.search(r"/chapter-(\d+)(?:-|$)", item.url)
            return int(match.group(1)) if match else item.id

        self.chapters.sort(key=chapter_number)
        for chapter_id, chapter in enumerate(self.chapters, start=1):
            chapter.id = chapter_id

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter.url)
        content = soup.select_one("article[data-chapter-slug]") or soup.select_one("article.prose")
        if not content:
            return ""
        for tag in content.select("script, style, img, nav, .not-prose"):
            tag.decompose()
        for heading in content.select("h1, h2, h3"):
            heading.decompose()
        for tag in list(content.find_all(True)):
            text = tag.get_text(" ", strip=True)
            if re.match(r"^Previously\s+on\b", text, re.IGNORECASE):
                tag.decompose()
            elif text.lower() == "details":
                tag.decompose()
        return self.cleaner.extract_contents(content)

