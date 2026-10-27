# -*- coding: utf-8 -*-
import re
from urllib.parse import urljoin
from lncrawl.core import Chapter, LegacyCrawler, Volume

class HoneyfeedCrawler(LegacyCrawler):
    base_url = ["https://www.honeyfeed.fm/"]

    def _meta(self, soup, selector):
        tag = soup.select_one(selector)
        return tag.get("content", "").strip() if tag else ""

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        self.novel_title = self._meta(soup, 'meta[property="og:title"]')
        self.novel_synopsis = self._meta(soup, 'meta[property="og:description"]')
        synopsis = soup.select_one("#wrap-details .wrap-novel-body")
        if synopsis:
            self.novel_synopsis = synopsis.get_text(" ", strip=True)
        image = self._meta(soup, 'meta[property="og:image"]')
        if image:
            self.novel_cover = self.absolute_url(image)
        author = soup.select_one('#wrap-details a[href*="/u/"]')
        self.novel_author = author.get_text(" ", strip=True) if author else ""

        catalogue = self.get_soup(self.novel_url.rstrip("/") + "/chapters")
        self.volumes.append(Volume(id=1))
        seen = set()
        for link in catalogue.select('a[href*="/chapters/"]'):
            href = link.get("href", "").split("#", 1)[0].strip()
            match = re.search(r"/chapters/(\d+)", href)
            if not match or href in seen:
                continue
            seen.add(href)
            number = link.select_one(".f12")
            title = link.select_one(".text-bold")
            number_text = number.get_text(" ", strip=True) if number else ""
            title_text = title.get_text(" ", strip=True) if title else ""
            self.chapters.append(Chapter(id=len(self.chapters) + 1, volume=1, title=" ".join((number_text, title_text)).strip(), url=urljoin(self.scraper.origin, href)))
        if not self.chapters:
            raise RuntimeError("Honeyfeed chapter catalogue was empty")
        self.chapters.sort(key=lambda chapter: int(re.search(r"/chapters/(\d+)", chapter.url).group(1)))
        for chapter_id, chapter in enumerate(self.chapters, 1):
            chapter.id = chapter_id

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter.url.split("#", 1)[0])
        content = soup.select_one("#chapter-body .wrap-body")
        if not content:
            return ""
        for tag in content.select("script, style, nav, img"):
            tag.decompose()
        for page in content.select('[id^="page-"]'):
            page.attrs.pop("id", None)
        return self.cleaner.extract_contents(content)
