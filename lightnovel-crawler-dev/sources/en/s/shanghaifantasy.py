# -*- coding: utf-8 -*-
import logging
import re

from lncrawl.core import Chapter, LegacyCrawler

logger = logging.getLogger(__name__)


class Shanghaifantasy(LegacyCrawler):
    base_url = ["https://shanghaifantasy.com/"]
    wp_json_novel = "https://shanghaifantasy.com/wp-json/wp/v2/novel/%s"
    wp_json_chapters = (
        "https://shanghaifantasy.com/wp-json/fiction/v1/chapters?category=%s&order=asc&per_page=%s"
    )

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)

        likebox = soup.select_one("#likebox[data-novel]")
        if not likebox:
            raise RuntimeError("Shanghai Fantasy novel id was not found")
        novel_id = likebox["data-novel"]

        total_chapters = 999
        for item in soup.select("div.grid p.text-sm"):
            text = item.get_text(" ", strip=True)
            if "Total Chapters" in text:
                match = re.search(r"\d+", text)
                if match:
                    total_chapters = int(match.group())
                break

        novel_json = self.get_response(self.wp_json_novel % novel_id).json()
        self.novel_title = novel_json["title"]["rendered"]

        for item in soup.select("div.grid p.text-sm"):
            text = item.get_text(" ", strip=True)
            if text.startswith("Author:"):
                self.novel_author = text.split(":", 1)[1].strip()
                break

        synopsis = soup.select_one("div[x-show*='Synopsis']")
        if synopsis:
            self.novel_synopsis = synopsis.get_text(" ", strip=True)

        cover = soup.select_one("img[class*='object-cover']")
        if cover and cover.get("src"):
            self.novel_cover = self.absolute_url(cover["src"])

        category_id = novel_json["categories"][0]
        chapters = self.get_response(
            self.wp_json_chapters % (category_id, total_chapters)
        ).json()

        for chapter in chapters:
            if chapter.get("locked"):
                continue
            self.chapters.append(
                Chapter(
                    id=len(self.chapters) + 1,
                    title=chapter.get("title", "").strip(),
                    url=chapter["permalink"],
                )
            )

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter["url"])
        post_id = soup.select_one("#comment_post_ID")
        if not post_id:
            raise RuntimeError("Shanghai Fantasy chapter post id was not found")

        data = self.get_response(
            "https://shanghaifantasy.com/wp-json/wp/v2/posts/%s"
            % post_id["value"]
        ).json()
        content = self.make_soup(data["content"]["rendered"])
        return self.cleaner.extract_contents(content.find("body") or content)
