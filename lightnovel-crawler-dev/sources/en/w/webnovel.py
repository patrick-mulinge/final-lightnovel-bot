# -*- coding: utf-8 -*-

import json
import logging
import re
from functools import cached_property
from time import time
from typing import Any, Iterable
from urllib.parse import urlencode, urlparse

from lncrawl.core import (
    Chapter,
    Novel,
    PageSoup,
    SearchResult,
    SoupTemplate,
    Volume,
)
from lncrawl.exceptions import LNException

logger = logging.getLogger(__name__)


class WebnovelCrawler(SoupTemplate):
    base_url = [
        "https://m.webnovel.com/",
        "https://www.webnovel.com/",
    ]

    def initialize(self) -> None:
        self.scraper.origin = "https://m.webnovel.com/"

        bad_text = [
            r"(\<pirate\>(.*?)\<\/pirate\>)"
            r"(Find authorized novels in Webnovel(.*)for visiting\.)",
        ]

        self.re_cleaner = re.compile("|".join(bad_text), re.M)

    @cached_property
    def csrf(self) -> str:
        self.scraper.get(f"{self.scraper.origin}stories/novel")
        return self.scraper.cookies.get("_csrfToken") or ""

    def search(self, query: str) -> Iterable[SearchResult]:
        try:
            params = {
                "_csrfToken": self.csrf,
                "pageIndex": 1,
                "encryptType": 3,
                "_fsae": 0,
                "keywords": query,
            }

            data = self.scraper.get_json(
                f"{self.scraper.origin}go/pcm/search/result?"
                f"{urlencode(params)}"
            )

            for book in data["data"]["bookInfo"]["bookItems"]:
                yield SearchResult(
                    title=book["bookName"],
                    url=f"{self.scraper.origin}book/{book['bookId']}",
                    info="%(categoryName)s | Score: %(totalScore)s" % book,
                )

        except Exception:
            logger.warning(
                "Failed to search Webnovel for %s",
                query,
                exc_info=True,
            )

    def _book_id_from_url(self, url: str) -> str:
        parts = [part for part in urlparse(url).path.split("/") if part]

        for part in parts:
            if "_" in part:
                possible_id = part.rsplit("_", 1)[-1]
                if possible_id.isdigit():
                    return possible_id

            if part.isdigit():
                return part

        raise LNException("Webnovel book ID was not found")

    def _next_payload(self, soup: PageSoup) -> dict[str, Any]:
        script = soup.select_one("#__NEXT_DATA__")

        if not script:
            raise LNException("Webnovel __NEXT_DATA__ payload was not found")

        try:
            return json.loads(script.get_text())
        except Exception as exc:
            raise LNException(
                "Webnovel __NEXT_DATA__ payload is invalid"
            ) from exc

    def _initial_state(self, soup: PageSoup) -> dict[str, Any]:
        payload = self._next_payload(soup)
        return payload.get("props", {}).get("initialState", {})

    def _page_data(self, soup: PageSoup) -> dict[str, Any]:
        payload = self._next_payload(soup)

        return (
            payload.get("props", {})
            .get("initialProps", {})
            .get("pageProps", {})
            .get("data", {})
        )

    def _find_value(self, value: Any, key: str) -> Any:
        if isinstance(value, dict):
            if key in value:
                return value[key]

            for child in value.values():
                result = self._find_value(child, key)
                if result is not None:
                    return result

        elif isinstance(value, list):
            for child in value:
                result = self._find_value(child, key)
                if result is not None:
                    return result

        return None

    def read_novel(self, novel: Novel) -> None:
        novel.novel_id = self._book_id_from_url(novel.url)

        novel_page = self.scraper.get_soup(
            self.absolute_url(novel.url)
        )

        state = self._initial_state(novel_page)
        entities = state.get("entities", {})

        books = entities.get("books", {})
        book_info = books.get(novel.novel_id, {})

        if not book_info:
            book_info = self._find_value(
                self._page_data(novel_page),
                "bookInfo",
            ) or {}

        novel.title = book_info.get("bookName", "")

        if not novel.title:
            title_tag = novel_page.select_one("h1, title")
            if title_tag:
                novel.title = title_tag.get_text(" ", strip=True)

        if not novel.title:
            raise LNException("Webnovel title was not found")

        novel.author = book_info.get("authorName", "")

        if not novel.author:
            novel.author = ", ".join(
                item.get("name", "")
                for item in book_info.get("authorItems", [])
                if item.get("name")
            )

        novel.cover_url = self.absolute_url(
            f"//book-pic.webnovel.com/bookcover/{novel.novel_id}"
            f"?coverUpdateTime{int(1000 * time())}"
            "&imageMogr2/thumbnail/600x"
        )

        catalog_url = (
            f"{self.scraper.origin}book/{novel.novel_id}/catalog"
        )

        catalog = self.scraper.get_soup(catalog_url)
        self.parse_chapter_catalog(catalog, novel)

    def parse_chapter_catalog(
        self,
        soup: PageSoup,
        novel: Novel,
    ) -> None:
        state = self._initial_state(soup)
        entities = state.get("entities", {})

        catalog_entities = entities.get("catalog", {})
        volume_entities = entities.get("volume", {})
        chapter_entities = entities.get("chapter", {})

        catalog = catalog_entities.get(novel.novel_id)

        if not catalog:
            raise LNException(
                "Webnovel catalog data was not found"
            )

        novel.volumes = []
        novel.chapters = []

        for volume_key in catalog.get("volumeItems", []):
            volume_data = volume_entities.get(volume_key, {})

            volume_id = volume_data.get(
                "volumeId",
                len(novel.volumes) + 1,
            )

            volume = Volume(
                id=int(volume_id),
                title=volume_data.get("volumeName", "")
                or f"Volume {volume_id}",
            )

            for chapter_key in volume_data.get("chapterItems", []):
                chapter_data = chapter_entities.get(chapter_key, {})

                chapter_id = str(
                    chapter_data.get("chapterId")
                    or chapter_key
                )

                chapter_number = chapter_data.get(
                    "chapterIndex",
                    len(novel.chapters) + 1,
                )

                chapter_name = (
                    chapter_data.get("chapterName")
                    or f"Chapter {chapter_number}"
                )

                chapter_url = (
                    f"{self.scraper.origin}book/"
                    f"{novel.novel_id}/{chapter_id}"
                )

                novel.chapters.append(
                    Chapter(
                        id=len(novel.chapters) + 1,
                        title=f"Chapter {chapter_number}: {chapter_name}",
                        url=chapter_url,
                        volume=volume.id,
                        novel_id=novel.novel_id,
                        chapter_id=chapter_id,
                    )
                )

            novel.volumes.append(volume)

        if not novel.chapters:
            raise LNException(
                "No Webnovel chapters were found"
            )

        logger.info(
            "Webnovel chapters found: %s",
            len(novel.chapters),
        )

    def _chapter_info_from_page(
        self,
        soup: PageSoup,
    ) -> dict[str, Any]:
        data = self._page_data(soup)

        chapter_info = self._find_value(
            data,
            "initChapterServer",
        )

        if isinstance(chapter_info, dict):
            nested = chapter_info.get("chapterInfo")

            if isinstance(nested, dict):
                return nested

        chapter_info = self._find_value(
            data,
            "chapterInfo",
        )

        if isinstance(chapter_info, dict):
            return chapter_info

        raise LNException(
            "Webnovel chapter information was not found"
        )

    def download_chapter(
        self,
        chapter: Chapter,
    ) -> None:
        soup = self.scraper.get_soup(chapter.url)
        info = self._chapter_info_from_page(soup)

        chapter.title = (
            info.get("chapterName")
            or chapter.title
            or f"Chapter {chapter.id}"
        )

        contents = info.get("contents") or []

        body_parts = [
            item.get("content", "")
            for item in contents
            if isinstance(item, dict)
            and item.get("content")
        ]

        if not body_parts and info.get("content"):
            body_parts = [info["content"]]

        chapter.body = "".join(
            self._format_content(part)
            for part in body_parts
        )

        if not chapter.body:
            raise LNException(
                f"Webnovel returned no content for "
                f"chapter {chapter.chapter_id}"
            )

    def _format_content(self, text: str) -> str:
        if not text:
            return ""

        if "<p>" not in text or "</p>" not in text:
            text = "".join(text.split("\r"))
            text = text.replace("<", "&lt;")
            text = text.replace(">", "&gt;")
            text = "</p><p>".join(
                line.strip()
                for line in text.split("\n")
                if line.strip()
            )

        text = self.re_cleaner.sub(
            "",
            f"<p>{text}</p>",
        )

        return text.strip()