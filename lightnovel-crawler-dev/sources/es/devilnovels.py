# -*- coding: utf-8 -*-
"""Crawler for Devil Novels."""

import re

from lncrawl.core import Chapter, LegacyCrawler, Volume


class DevilNovelsCrawler(LegacyCrawler):
    base_url = ["https://devilnovels.com/"]
    ajax_url = "https://devilnovels.com/wp-admin/admin-ajax.php"

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        title = soup.select_one("h1, .entry-title")
        self.novel_title = title.get_text(" ", strip=True) if title else ""
        if not self.novel_title:
            meta_title = soup.select_one("meta[property='og:title'], meta[name='title']")
            self.novel_title = meta_title.get("content", "").strip() if meta_title else ""
        self.novel_title = re.sub(r"\s*[-|]\s*Devilnovels.*$", "", self.novel_title, flags=re.I).strip()
        author = soup.select_one(".author, [rel='author'], [itemprop='author']")
        self.novel_author = author.get_text(" ", strip=True) if author else ""
        meta_description = soup.select_one("meta[property='og:description'], meta[name='description']")
        self.novel_synopsis = meta_description.get("content", "").strip() if meta_description else ""
        cover = soup.select_one("meta[property='og:image'], .novel-cover img, img.wp-post-image")
        if cover:
            image_url = cover.get("content") or cover.get("src")
            if image_url:
                self.novel_cover = image_url

        self.volumes.append(Volume(id=1, title="Volume 1"))
        self.chapters = []
        seen = set()
        for page in range(1, 70):
            response = self.post_response(
                self.ajax_url,
                data={
                    "action": "dv_load_chapters",
                    "cat_id": "38",
                    "page": str(page),
                    "search": "",
                },
            ).json()
            payload = response.get("data", response)
            for item in payload.get("chapters", []):
                title = item.get("title", "")
                match = re.search(r"(?:chapter|cap[ií]tulo)\s+(\d+)", title, re.I)
                href = item.get("link", "")
                if not match or not href:
                    continue
                number = int(match.group(1))
                if number in seen:
                    continue
                seen.add(number)
                self.chapters.append(Chapter(
                    id=number,
                    volume=1,
                    title=title,
                    url=href,
                ))
        self.chapters.sort(key=lambda chapter: chapter.id)
        if not self.chapters:
            raise RuntimeError("Devil Novels chapter catalogue was empty")

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter.url)
        content = soup.select_one("article.entry-content, .entry-content, article")
        if not content:
            return ""
        for tag in content.select("script, style, nav, button, .sharedaddy, .jp-relatedposts, .ads, .ad, img, figure"):
            tag.decompose()
        return self.cleaner.extract_contents(content)



