# -*- coding: utf-8 -*-
import json
import logging
import re
import time

from lncrawl.core import Chapter, LegacyCrawler, Volume

logger = logging.getLogger(__name__)


class FenriRealm(LegacyCrawler):
    base_url = [
        "https://fenrirealm.com/",
    ]

    def search_novel(self, query):
        search_url = (
            f"{self.scraper.origin.rstrip('/')}/api/novels/filter?page=1&per_page=10&search={query}"
        )
        response = self.get_response(search_url).text
        data = json.loads(response)

        results = []
        if data and "data" in data:
            for novel in data["data"]:
                results.append(
                    {
                        "title": novel["title"],
                        "url": f"{self.scraper.origin.rstrip('/')}/series/{novel['slug']}",
                        "info": f"Author: {novel['user']['name']}, Status: {novel['status']}",
                    }
                )

        return results

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)

        # fenrirealm.com migrated to SvelteKit; novel data is now embedded as a
        # JS object literal inside a <script> tag rather than `const data = ...`
        script_text = ""
        for script in soup.find_all("script"):
            if script.string and "seriesData" in script.string:
                script_text = script.string
                break

        if not script_text:
            raise Exception("Could not find seriesData in page scripts")

        title_match = re.search(r'seriesData:\{.*?title:"([^"]+)"', script_text)
        if title_match:
            self.novel_title = title_match.group(1)

        author_match = re.search(r'user:\{username:"[^"]+",name:"([^"]+)"\}', script_text)
        if author_match:
            self.novel_author = author_match.group(1)

        cover_match = re.search(r'cover:"(storage/[^"]+)"', script_text)
        if cover_match:
            self.novel_cover = self.scraper.origin.rstrip("/") + "/" + cover_match.group(1)

        synopsis_match = re.search(r'description:"((?:[^"\\]|\\.)*)"', script_text)
        if synopsis_match:
            self.novel_synopsis = synopsis_match.group(1).encode().decode("unicode_escape")

        tags_match = re.search(r"tags:\[([^\]]+)\]", script_text)
        if tags_match:
            self.novel_tags = re.findall(r'name:"([^"]+)"', tags_match.group(1))

        # Extract slug from URL; guard against URLs that include a chapter number
        url_parts = self.novel_url.rstrip("/").split("/")
        novel_slug = url_parts[-1]
        if novel_slug.isdigit():
            novel_slug = url_parts[-2]

        chapter_list_url = f"{self.scraper.origin.rstrip('/')}/api/novels/chapter-list/{novel_slug}"
        response = self.get_response(chapter_list_url).text
        chapters_data = json.loads(response)

        chapters_data.sort(key=lambda x: x["number"])

        free_chapters = [ch for ch in chapters_data if ch["locked"]["price"] == 0]

        for chapter in free_chapters:
            chap_id = len(self.chapters) + 1
            vol_id = 1 + len(self.chapters) // 100

            if chap_id % 100 == 1:
                self.volumes.append(Volume(id=vol_id))

            slug = chapter.get("slug", "")
            name = chapter.get("name", "")
            title = chapter.get("title", "")

            chapter_title = name
            if title and title.strip():
                chapter_title += f" - {title}"

            chapter_url = f"{self.scraper.origin.rstrip('/')}/series/{novel_slug}/{slug}"

            self.chapters.append(
                Chapter(
                    id=chap_id,
                    volume=vol_id,
                    title=chapter_title,
                    url=chapter_url,
                )
            )

    def download_chapter_body(self, chapter):
        page = self.render_soup(
            chapter.url,
            wait_for="[id^='reader-area-'] p",
            timeout=120,
        )
        chapter_body = page.select_one("[id^='reader-area-']") if page else None
        if chapter_body:
            for tag in chapter_body.find_all("style"):
                tag.decompose()

            for tag in chapter_body.find_all(attrs={"aria-hidden": True}):
                if str(tag.get("aria-hidden", "")).strip().lower() == "true":
                    tag.decompose()

        if not chapter_body:
            return ""

        # The reader toolbar and access-check widgets share the reader-area
        # container. Chapter paragraphs are direct <p> children, so discard
        # the surrounding controls before extracting the text.
        for child in list(chapter_body.children):
            if getattr(child, "name", None) != "p":
                child.decompose()

        body = self.cleaner.extract_contents(chapter_body)
        time.sleep(10)
        return body




