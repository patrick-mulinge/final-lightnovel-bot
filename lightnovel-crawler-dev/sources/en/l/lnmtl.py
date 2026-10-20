# -*- coding: utf-8 -*-
"""Crawler for LNMTL novels."""

from concurrent import futures
import json
import logging
import re

from lncrawl.core import Chapter, LegacyCrawler

logger = logging.getLogger(__name__)


class LNMTLCrawler(LegacyCrawler):
    has_mtl = True
    base_url = "https://lnmtl.com/"

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)

        title_tag = soup.select_one(".novel .media .novel-name")
        if not title_tag:
            raise RuntimeError("LNMTL novel title was not found")
        title = title_tag.get_text(" ", strip=True)
        self.novel_title = title.rsplit(" ", 1)[0] if title else title

        try:
            cover = soup.select_one(".novel .media img[title], .novel img.media-object")
            if cover and cover.get("src"):
                self.novel_cover = self.absolute_url(cover["src"])
        except Exception:
            logger.debug("LNMTL cover was not available", exc_info=True)

        self.parse_volume_list(soup)
        self.volumes.sort(key=lambda volume: volume["id"])
        self.download_chapter_list()

    def parse_volume_list(self, soup):
        """Read the embedded volume JSON without relying on script position."""
        self.volumes = []
        script_text = ""
        for script in soup.select("script"):
            text = script.string or script.get_text()
            if "lnmtl.volumes" in text:
                script_text = text
                break

        match = re.search(r"lnmtl\.volumes\s*=\s*(\[.*?\])\s*;", script_text, re.DOTALL)
        if not match:
            raise RuntimeError("LNMTL volume data was not found in the page")

        volumes = json.loads(match.group(1))
        if not isinstance(volumes, list):
            raise RuntimeError("LNMTL volume data was not a list")

        for index, volume in enumerate(volumes, start=1):
            volume_id = volume.get("id")
            if volume_id is None:
                continue
            self.volumes.append(
                {
                    "id": index,
                    "title": (volume.get("title") or "").strip(),
                    "download_id": volume_id,
                }
            )

        if not self.volumes:
            raise RuntimeError("LNMTL returned no volumes")

    def download_chapter_list(self):
        futures_to_wait = [
            self.executor.submit(self.download_chapters_per_volume, volume)
            for volume in self.volumes
        ]
        chapters_by_volume = {}
        for future in futures.as_completed(futures_to_wait):
            volume_id, chapters = future.result()
            chapters_by_volume[volume_id] = chapters

        for volume in self.volumes:
            for chapter in chapters_by_volume.get(volume["id"], []):
                chapter = dict(chapter)
                chapter["id"] = len(self.chapters) + 1
                chapter["volume"] = volume["id"]
                self.chapters.append(Chapter(**chapter))

    def download_chapters_per_volume(self, volume, page=1):
        url = self.absolute_url(f"/chapter?page={page}&volumeId={volume['download_id']}")
        result = self.get_json(url)
        chapters = []

        for item in result.get("data", []):
            title = (item.get("title") or "").strip()
            number = item.get("number")
            if number:
                title = f"#{number} {title}".strip()
            chapter_url = item.get("site_url")
            if chapter_url:
                chapters.append({"title": title, "url": chapter_url})

        if page == 1:
            for next_page in range(2, int(result.get("last_page", 1)) + 1):
                chapters.extend(self.download_chapters_per_volume(volume, next_page))
            return volume["id"], chapters

        return chapters

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter["url"])
        sentences = soup.select(".chapter-body .translated")
        paragraphs = []

        for sentence in sentences:
            text = sentence.get_text(" ", strip=True)
            text = re.sub(r"\s+([,.;:!?])", r"\1", text)
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                paragraphs.append(f"<p>{text}</p>")

        return "\n".join(paragraphs)

