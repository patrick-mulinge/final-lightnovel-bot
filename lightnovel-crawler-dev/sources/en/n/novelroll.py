# -*- coding: utf-8 -*-
"""Crawler for novelroll.com."""

import json
import re

from bs4 import BeautifulSoup

from lncrawl.core import Chapter, LegacyCrawler, SearchResult, Volume


class NovelrollCrawler(LegacyCrawler):
    base_url = ["https://novelroll.com/"]
    can_search = True

    def search_novel(self, query):
        page = self.get_soup(self.scraper.origin.rstrip("/") + "/")
        component = page.select_one('[wire\\:name="header.search"][wire\\:id]')
        if not component:
            return
        snapshot = component.get("wire:snapshot")
        if not snapshot:
            return
        token = page.select_one("meta[name=csrf-token]")
        payload = {
            "_token": token.get("content", "") if token else "",
            "components": [{
                "snapshot": snapshot,
                "updates": {"search": query},
                "calls": [{"method": "$commit", "params": [], "metadata": {"type": "model.live"}}],
            }],
        }
        response = self.post_response(
            self.scraper.origin.rstrip("/") + "/livewire-5cdd1b42/update",
            json=payload,
            headers={"X-Livewire": "1", "X-Requested-With": "XMLHttpRequest"},
        ).json()
        html = "".join(
            (item.get("effects") or {}).get("html", "")
            for item in response.get("components", [])
        )
        results = BeautifulSoup(html, "html.parser")
        seen = set()
        for link in results.select("a[href]"):
            href = link.get("href", "")
            title = link.get("title") or link.get_text(" ", strip=True)
            if not href or not title or href in seen:
                continue
            if not any(path in href for path in ("/book/", "/novel/", "/series/")):
                continue
            seen.add(href)
            yield SearchResult(title=title, url=self.absolute_url(href))

    def _json_ld_book(self, soup):
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.get_text())
            except (TypeError, json.JSONDecodeError):
                continue
            items = data.get("@graph", []) if isinstance(data, dict) else data
            items = items if isinstance(items, list) else [items]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "Book":
                    return item
        return {}

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        book = self._json_ld_book(soup)
        self.novel_title = str(book.get("name", "")).strip()
        self.novel_synopsis = str(book.get("description", "")).strip()
        visible_synopsis = soup.select_one('div[x-ref="content"] div.text-left > div')
        if visible_synopsis:
            self.novel_synopsis = visible_synopsis.get_text(" ", strip=True) or self.novel_synopsis
        if book.get("image"):
            self.novel_cover = self.absolute_url(str(book["image"]))
        author = book.get("author", "")
        self.novel_author = str(author.get("name", "") if isinstance(author, dict) else author).strip()

        toc = self.get_soup(self.novel_url.rstrip("/") + "?toc")
        book_path = self.novel_url.rstrip("/").split("novelroll.com", 1)[-1].rstrip("/")
        if not book_path.startswith("/"):
            book_path = "/" + book_path
        entries = [
            (item.get("url", ""), item.get("name", ""))
            for item in book.get("hasPart", [])
            if isinstance(item, dict)
        ]
        entries.extend(
            (link.get("href", ""), link.get("title") or link.get_text(" ", strip=True))
            for link in toc.select('a[href*="/chapter-"]')
        )
        self.volumes.append(Volume(id=1))
        seen = set()
        for href, title in entries:
            href = href.strip()
            if href.startswith("https://novelroll.com"):
                href = href.split("novelroll.com", 1)[-1]
            if not href or href in seen or not href.startswith(book_path + "/chapter-"):
                continue
            if not title.lower().startswith("chapter"):
                continue
            seen.add(href)
            self.chapters.append(Chapter(id=len(self.chapters) + 1, volume=1, title=title.strip(), url=self.absolute_url(href)))
        if not self.chapters:
            raise RuntimeError("NovelRoll chapter catalogue was empty")

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
        for tag in content.select("script, style, img, nav, .not-prose, h1, h2, h3"):
            tag.decompose()
        for tag in list(content.find_all(True)):
            text = tag.get_text(" ", strip=True)
            if re.match(r"^Previously\s+on\b", text, re.IGNORECASE) or text.lower() == "details":
                tag.decompose()
        return self.cleaner.extract_contents(content)
