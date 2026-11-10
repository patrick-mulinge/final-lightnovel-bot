# -*- coding: utf-8 -*-
"""Crawler for Dragonholic Translations."""

from urllib.parse import urljoin, urlparse

from lncrawl.core import Chapter, LegacyCrawler, Volume


class DragonholicTranslationsCrawler(LegacyCrawler):
    base_url = ["https://dragonholictranslations.com/"]
    api_url = "https://dragonholictranslations.com/api/chapters"

    def _headers(self, referer):
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/141.0.0.0 Safari/537.36"
            ),
            "X-Requested-With": "XMLHttpRequest",
        }

    def _series_id(self, soup):
        body = soup.select_one("body")
        if body:
            classes = body.get("class", [])
            for class_name in classes:
                if class_name.startswith("postid-"):
                    return class_name.removeprefix("postid-")
        shortlink = soup.select_one('link[rel="shortlink"]')
        if shortlink:
            query = urlparse(shortlink.get("href", "")).query
            if query.startswith("p="):
                return query[2:]
        raise RuntimeError("Dragonholic series ID was not found")

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        self.cleaner.bad_tags.update(["script", "style", "nav", "form", "button"])
        self.cleaner.bad_css.update([".ads", ".ad", "[id*='ad-']", "[class*='comment']"])

        title = soup.select_one("h1")
        self.novel_title = title.get_text(" ", strip=True) if title else ""
        author = soup.select_one("a[href*='/author/'], .author-name")
        self.novel_author = author.get_text(" ", strip=True) if author else ""
        synopsis = soup.select_one("[x-ref='synopsis']")
        self.novel_synopsis = (
            self.cleaner.extract_contents(synopsis) if synopsis else ""
        )
        cover = soup.select_one("meta[property='og:image']")
        if cover:
            self.novel_cover = urljoin(self.scraper.origin, cover.get("content", ""))
        if not self.novel_cover:
            image = soup.select_one("img[width='225'][height='300'], main img[src*='wp-content/uploads/']:not([src*='Logo'])")
            if image:
                self.novel_cover = urljoin(self.scraper.origin, image.get("src", ""))

        series_id = self._series_id(soup)
        response = self.get_response(
            self.api_url,
            params={"series_id": series_id, "sort_order": "asc", "load_all": "1"},
            headers=self._headers(self.novel_url),
        ).json()
        chapters = response.get("chapters", [])
        if not response.get("success") or not chapters:
            raise RuntimeError("Dragonholic chapter catalogue was empty")

        self.volumes.append(Volume(id=1, title="Volume 1"))
        for item in chapters:
            if item.get("is_premium"):
                continue
            number = int(item.get("chapter_order") or len(self.chapters) + 1)
            slug = item.get("slug") or f"chapter-{number}"
            title = item.get("name") or f"Chapter {number}"
            subtitle = item.get("subtitle")
            if subtitle and subtitle not in title:
                title = f"{title} - {subtitle}"
            self.chapters.append(
                Chapter(
                    id=number,
                    volume=1,
                    title=title,
                    url=urljoin(self.novel_url.rstrip("/") + "/", f"{slug}/"),
                )
            )
        self.chapters.sort(key=lambda chapter: chapter.id)

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter.url)
        content = soup.select_one(".chapter-content")
        if not content:
            return ""
        for tag in content.select("script, style, nav, form, button, img, .ads, .ad"):
            tag.decompose()
        return self.cleaner.extract_contents(content)