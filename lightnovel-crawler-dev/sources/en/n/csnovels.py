# -*- coding: utf-8 -*-
"""Crawler for CS Novels book and chapter pages."""

import json
from html import escape
import re
from urllib.parse import urljoin, urlparse

from lncrawl.core import Chapter, LegacyCrawler, Volume


class CsnovelsCrawler(LegacyCrawler):
    base_url = ["https://csnovels.com/"]

    def read_novel_info(self):
        self.novel_title = re.sub(r"[-_]+", " ", self.novel_url.rstrip("/").rsplit("/", 1)[-1]).title()
        soup = self.get_soup(self.novel_url)
        metadata = self._json_ld(soup)

        title = soup.select_one("h1.bookTitle, .bookTitle")
        slug_match = re.search(r"/book/([^/?#]+)/?", urlparse(self.novel_url).path)
        slug = slug_match.group(1) if slug_match else ""
        api_origin = self.scraper.origin.rstrip("/")
        book_response = self.get_json(f"{api_origin}/getSingleBook?slug={slug}") if slug else {}
        book_data = book_response.get("book", {}) if isinstance(book_response, dict) else {}
        fallback_title = slug.replace("-", " ").title()
        self.novel_title = book_data.get("title") or (title.get_text(" ", strip=True) if title else "") or metadata.get("name", "") or fallback_title or self.novel_title
        description = soup.select_one('meta[name="description"], meta[property="og:description"]')
        self.novel_synopsis = book_data.get("description", "") or (
            description.get("content", "").strip() if description else metadata.get("description", "")
        )
        api_cover = book_data.get("coverimage") or book_data.get("coverImage")
        if api_cover:
            self.novel_cover = urljoin(api_origin, api_cover)
        cover = soup.select_one('meta[property="og:image"]')
        if cover and cover.get("content") and not getattr(self, "novel_cover", ""):
            self.novel_cover = urljoin(self.scraper.origin, cover["content"])

        self.volumes.append(Volume(id=1, title="Volume 1"))
        chapters = {}
        for item in metadata.get("hasPart", []):
            if not isinstance(item, dict):
                continue
            url = item.get("url", "")
            match = re.search(r"/chapter/[^/]+/(\d+)/?$", url)
            if match:
                chapters[int(match.group(1))] = (item.get("name", ""), url)

        for link in soup.select('#chapter-list a[href*="/chapter/"]'):
            href = link.get("href", "")
            match = re.search(r"/chapter/[^/]+/(\d+)/?$", href)
            if match:
                number = int(match.group(1))
                chapters[number] = (link.get_text(" ", strip=True), urljoin(self.scraper.origin, href))

        total = soup.select_one(".total-chapters, #number-of-chapters, #chapter-count")
        total_match = re.search(r"\d+", total.get_text(" ", strip=True) if total else "")
        slug_match = re.search(r"/book/([^/?#]+)/?", urlparse(self.novel_url).path)
        slug = slug_match.group(1) if slug_match else ""
        api_origin = self.scraper.origin.rstrip("/")
        book_response = self.get_json(f"{api_origin}/getSingleBook?slug={slug}") if slug else {}
        book_data = book_response.get("book", {}) if isinstance(book_response, dict) else {}
        if not slug and chapters:
            slug = next(iter(chapters.values()))[1].split("/chapter/", 1)[1].rsplit("/", 1)[0]
        if slug:
            total_count = int(book_data.get("chaptercount") or 0)
            if not total_count and total_match:
                total_count = int(total_match.group(0))
            for number in range(1, total_count + 1):
                chapters.setdefault(number, (f"Chapter {number}", f"{self.scraper.origin}/chapter/{slug}/{number}"))

        for number in sorted(chapters):
            title, url = chapters[number]
            self.chapters.append(Chapter(id=number, volume=1, title=title or f"Chapter {number}", url=url))
        if not self.chapters:
            raise RuntimeError("CS Novels chapter catalogue was empty")

    def download_chapter_body(self, chapter):
        match = re.search(r"/chapter/([^/]+)/([0-9]+)/?", chapter.url)
        if match:
            slug, number = match.groups()
            api_url = f"{self.scraper.origin.rstrip(chr(47))}/getsinglechapter?slug={slug}&chapternumber={number}"
            data = self.get_json(api_url)
            if isinstance(data, dict) and data.get("chaptercontent"):
                return self._format_chapter_text(data["chaptercontent"])
        soup = self.get_soup(chapter.url)
        content = soup.select_one(f"#chapter-{chapter.id}, #chapters-container .chapter")
        if not content:
            text = self._json_ld(soup).get("text", "")
            return self._format_chapter_text(text)
        for tag in content.select("script, style, nav, button, img, .ad-container, ins, iframe"):
            tag.decompose()
        heading = content.select_one(".chapter-title")
        if heading:
            heading.decompose()
        return self.cleaner.extract_contents(content)

    @staticmethod
    @staticmethod
    def _format_chapter_text(text):
        paragraphs = re.split(r"\n\s*\n", text.replace("noveldrama", "").strip())
        formatted = []
        for paragraph in paragraphs:
            if paragraph.strip():
                normalized = re.sub(r"\\s+", " ", paragraph).strip()
                formatted.append("<p>" + escape(normalized) + "</p>")
        return "".join(formatted)

    @staticmethod
    def _json_ld(soup):
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or script.get_text())
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("@type") in ("Book", "Chapter"):
                return data
        return {}
















