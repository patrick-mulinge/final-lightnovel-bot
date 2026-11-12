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

    def _diagnose(self, soup: PageSoup) -> str:
        """Short description of what the server actually returned."""
        title = soup.select_one("title")
        title = title.get_text(" ", strip=True) if title else "no <title>"
        body = soup.get_text(" ", strip=True)[:160]
        return f"page title={title!r}, body starts={body!r}"

    def _next_payload(self, soup: PageSoup) -> dict[str, Any]:
        """Return __NEXT_DATA__ if the page has it, else {} (never raises)."""
        script = soup.select_one("#__NEXT_DATA__")
        if not script:
            return {}
        try:
            return json.loads(script.get_text())
        except Exception:
            logger.warning("Webnovel __NEXT_DATA__ is invalid JSON")
            return {}

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

    def _meta(self, soup: PageSoup, name: str) -> str:
        tag = soup.select_one(
            f'meta[property="{name}"], meta[name="{name}"]'
        )
        return (tag.get("content") or "").strip() if tag else ""

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

        novel_page = self.scraper.get_soup(self.absolute_url(novel.url))

        # --- metadata: old __NEXT_DATA__ first, then the rendered DOM ---
        state = self._initial_state(novel_page)
        book_info = (
            state.get("entities", {}).get("books", {}).get(novel.novel_id)
            or self._find_value(self._page_data(novel_page), "bookInfo")
            or {}
        )

        novel.title = book_info.get("bookName", "")
        if not novel.title:
            h1 = novel_page.select_one("h1")
            if h1:
                novel.title = h1.get_text(" ", strip=True)
        if not novel.title:
            novel.title = self._meta(novel_page, "og:title")
        if not novel.title:
            raise LNException(
                "Webnovel title was not found - "
                + self._diagnose(novel_page)
            )

        novel.author = book_info.get("authorName", "") or ", ".join(
            item.get("name", "")
            for item in book_info.get("authorItems", [])
            if item.get("name")
        )
        if not novel.author:
            novel.author = self._meta(novel_page, "og:author")
        if not novel.author:
            tag = novel_page.select_one('[class*="NewBookInfo_author"]')
            if tag:
                novel.author = re.sub(
                    r"^(oleh|by)\s+", "", tag.get_text(" ", strip=True),
                    flags=re.I,
                )

        novel.cover_url = self.absolute_url(
            f"//book-pic.webnovel.com/bookcover/{novel.novel_id}"
            f"?coverUpdateTime{int(1000 * time())}"
            "&imageMogr2/thumbnail/600x"
        )

        self._read_catalog(novel, novel_page)

    # ------------------------------------------------------------------
    # catalog: the new site no longer ships it in __NEXT_DATA__, so try
    # the JSON API first, then the old payload.
    # ------------------------------------------------------------------
    def _read_catalog(self, novel: Novel, novel_page: PageSoup) -> None:
        novel.volumes = []
        novel.chapters = []

        errors = []
        for name, method in (
            ("api", self._catalog_from_api),
            ("next_data", self._catalog_from_next_data),
        ):
            try:
                method(novel, novel_page)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                logger.warning("Webnovel catalog via %s failed: %s", name, exc)
            if novel.chapters:
                logger.info(
                    "Webnovel chapters found via %s: %s",
                    name, len(novel.chapters),
                )
                return

        raise LNException(
            "No Webnovel chapters were found (" + "; ".join(errors) + ")"
        )

    def _add_chapter(self, novel: Novel, volume_id: int, cid: str,
                     number: Any, name: str) -> None:
        novel.chapters.append(
            Chapter(
                id=len(novel.chapters) + 1,
                title=f"Chapter {number}: {name}" if name else f"Chapter {number}",
                url=f"{self.scraper.origin}book/{novel.novel_id}/{cid}",
                volume=volume_id,
                novel_id=novel.novel_id,
                chapter_id=str(cid),
            )
        )

    def _catalog_from_api(self, novel: Novel, _page: PageSoup) -> None:
        params = {
            "_csrfToken": self.csrf,
            "bookId": novel.novel_id,
            "pageIndex": 0,
            "encryptType": 3,
            "_fsae": 0,
        }
        data = self.scraper.get_json(
            f"{self.scraper.origin}go/pcm/chapter/get-chapter-list?"
            f"{urlencode(params)}"
        )
        root = data.get("data", data)

        volume_items = root.get("volumeItems")
        if volume_items:
            groups = [
                (v.get("volumeId") or i + 1, v.get("chapterItems", []))
                for i, v in enumerate(volume_items)
            ]
        else:
            groups = [(1, root.get("chapterItems", []))]

        for volume_id, items in groups:
            novel.volumes.append(Volume(id=int(volume_id), title=f"Volume {volume_id}"))
            for item in items:
                cid = item.get("chapterId") or item.get("id")
                if not cid:
                    continue
                number = item.get("chapterIndex") or item.get("index") or len(novel.chapters) + 1
                name = item.get("chapterName") or item.get("name") or ""
                self._add_chapter(novel, int(volume_id), cid, number, name)

    def _catalog_from_next_data(self, novel: Novel, _page: PageSoup) -> None:
        soup = self.scraper.get_soup(f"{self.scraper.origin}book/{novel.novel_id}/catalog")
        self.parse_chapter_catalog(soup, novel)

    def parse_chapter_catalog(self, soup: PageSoup, novel: Novel) -> None:
        """Legacy parser for the old __NEXT_DATA__ redux state."""
        entities = self._initial_state(soup).get("entities", {})
        catalog = entities.get("catalog", {}).get(novel.novel_id)
        if not catalog:
            raise LNException("catalog data not in __NEXT_DATA__ - " + self._diagnose(soup))

        volume_entities = entities.get("volume", {})
        chapter_entities = entities.get("chapter", {})
        for volume_key in catalog.get("volumeItems", []):
            vd = volume_entities.get(volume_key, {})
            vid = int(vd.get("volumeId", len(novel.volumes) + 1))
            novel.volumes.append(Volume(id=vid, title=vd.get("volumeName", "") or f"Volume {vid}"))
            for ck in vd.get("chapterItems", []):
                cd = chapter_entities.get(ck, {})
                cid = str(cd.get("chapterId") or ck)
                num = cd.get("chapterIndex", len(novel.chapters) + 1)
                self._add_chapter(novel, vid, cid, num, cd.get("chapterName") or "")

    # ------------------------------------------------------------------
    def _chapter_from_next_data(self, soup: PageSoup) -> tuple[str, list[str]]:
        data = self._page_data(soup)
        info = self._find_value(data, "initChapterServer")
        if isinstance(info, dict) and isinstance(info.get("chapterInfo"), dict):
            info = info["chapterInfo"]
        else:
            info = self._find_value(data, "chapterInfo")
        if not isinstance(info, dict):
            return "", []
        parts = [
            i.get("content", "")
            for i in info.get("contents") or []
            if isinstance(i, dict) and i.get("content")
        ]
        if not parts and info.get("content"):
            parts = [info["content"]]
        return info.get("chapterName") or "", parts

    def _chapter_from_dom(self, soup: PageSoup) -> tuple[str, list[str]]:
        box = soup.select_one('div[id^="content-"]')
        if not box:
            return "", []
        h1 = soup.select_one("h1")
        title = h1.get_text(" ", strip=True) if h1 else ""
        paras = [
            p.get_text(" ", strip=True)
            for p in box.select("p")
            if p.get_text(strip=True)
        ]
        return title, ["\n".join(paras)] if paras else []

    def download_chapter(self, chapter: Chapter) -> None:
        soup = self.scraper.get_soup(chapter.url)

        title, parts = self._chapter_from_next_data(soup)
        if not parts:
            title, parts = self._chapter_from_dom(soup)

        if not parts:
            raise LNException(
                f"Webnovel returned no content for chapter "
                f"{chapter.chapter_id} - " + self._diagnose(soup)
            )

        chapter.title = title or chapter.title or f"Chapter {chapter.id}"
        chapter.body = "".join(self._format_content(p) for p in parts)

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