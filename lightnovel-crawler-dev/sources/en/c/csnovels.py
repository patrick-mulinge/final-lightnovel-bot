# -*- coding: utf-8 -*-
"""Corrected CS Novels crawler replacement."""

import json
import re
from html import escape
from urllib.parse import quote, urljoin, urlparse

from lncrawl.core import Chapter, LegacyCrawler, SearchResult, Volume


class CsnovelsCrawler(LegacyCrawler):
    base_url = ["https://csnovels.com/"]
    can_search = True

    def search_novel(self, query):
        soup = self.get_soup(f"{self.scraper.origin.rstrip('/')}/book-search?by=search&query={quote(query)}")
        seen = set()
        for link in soup.select("a[href*='/book/']"):
            href = link.get("href", "").split("#", 1)[0]
            title = link.get_text(" ", strip=True)
            if href and title and href not in seen:
                seen.add(href)
                yield SearchResult(title=title, url=urljoin(self.scraper.origin, href))

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        metadata = self._json_ld(soup)
        slug_match = re.search(r"/book/([^/?#]+)/?", urlparse(self.novel_url).path)
        slug = slug_match.group(1) if slug_match else ""
        response = self.get_json(f"{self.scraper.origin.rstrip('/')}/getSingleBook?slug={slug}") if slug else {}
        book = response.get("book", {}) if isinstance(response, dict) else {}
        heading = soup.select_one("h1.bookTitle, .bookTitle")
        self.novel_title = book.get("title") or (heading.get_text(" ", strip=True) if heading else "") or metadata.get("name", "") or slug.replace("-", " ").title()
        description = soup.select_one('meta[name="description"], meta[property="og:description"]')
        self.novel_synopsis = book.get("description", "") or (description.get("content", "").strip() if description else metadata.get("description", ""))
        cover = book.get("coverimage") or book.get("coverImage") or (soup.select_one('meta[property="og:image"]') or {}).get("content", "")
        if cover:
            self.novel_cover = urljoin(self.scraper.origin, cover)
        author = soup.select_one('meta[property="og:novel:author"]')
        if author:
            self.novel_author = author.get("content", "").strip()
        self.volumes.append(Volume(id=1, title="Volume 1"))
        chapters = {}
        for link in soup.select('#chapter-list a[href*="/chapter/"]'):
            match = re.search(r"/chapter/[^/]+/(\d+)/?$", link.get("href", ""))
            if match:
                chapters[int(match.group(1))] = (link.get_text(" ", strip=True), urljoin(self.novel_url, link["href"]))
        count = int(book.get("chaptercount") or 0)
        for number in range(1, count + 1):
            chapters.setdefault(number, (f"Chapter {number}", f"{self.scraper.origin}/chapter/{slug}/{number}"))
        for number in sorted(chapters):
            title, href = chapters[number]
            self.chapters.append(Chapter(id=number, volume=1, title=title or f"Chapter {number}", url=href))
        if not self.chapters:
            raise RuntimeError("CS Novels chapter catalogue was empty")

    def download_chapter_body(self, chapter):
        match = re.search(r"/chapter/([^/]+)/([0-9]+)/?", chapter.url)
        if match:
            slug, number = match.groups()
            data = self.get_json(f"{self.scraper.origin.rstrip('/')}/getsinglechapter?slug={slug}&chapternumber={number}")
            if isinstance(data, dict) and data.get("chaptercontent"):
                return self._format_chapter_text(data["chaptercontent"])
        soup = self.get_soup(chapter.url)
        content = soup.select_one(f"#chapter-{chapter.id}, #chapters-container .chapter")
        return self.cleaner.extract_contents(content) if content else self._format_chapter_text(self._json_ld(soup).get("text", ""))

    @staticmethod
    def _format_chapter_text(text):
        normalized = []
        for paragraph in re.split(r"\n\s*\n", text.replace("noveldrama", "").strip()):
            paragraph = re.sub(r"\s+", " ", paragraph).strip()
            if paragraph:
                normalized.append(f"<p>{escape(paragraph)}</p>")
        return "".join(normalized)

    @staticmethod
    def _json_ld(soup):
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or script.get_text())
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                return data
        return {}
