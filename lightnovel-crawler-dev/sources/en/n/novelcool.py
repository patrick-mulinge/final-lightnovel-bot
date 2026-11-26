# -*- coding: utf-8 -*-
"""Crawler for NovelCool."""

from lncrawl.core import Chapter, SearchResult, SoupTemplate


class NovelCool(SoupTemplate):
    base_url = "https://www.novelcool.com/"
    has_manga = True
    can_search = True
    novel_title_selector = "h1.bookinfo-title"
    novel_author_selector = ".bookinfo-author a"
    novel_cover_selector = ".bookinfo-pic img"
    novel_synopsis_selector = ".bk-summary-txt"
    novel_tags_selector = ".bookinfo-category-list a"
    chapter_list_selector = ".chapter-item-list a"
    chapter_title_selector = ".chapter-item-title"
    chapter_list_reverse = True

    def search(self, query):
        data = self.post_response(
            f"{self.scraper.origin.rstrip('/')}/ajax/search/",
            data={"term": query, "secret": "cb4f178fa20e644fb70747574732d1b6", "appId": "201612090714001", "user_id": "", "token": ""},
        ).json()
        for item in data if isinstance(data, list) else []:
            title, href = item.get("name"), item.get("url")
            if title and href:
                yield SearchResult(title=title, url=self.absolute_url(href), author=item.get("author", ""))

    def initialize(self):
        self.cleaner.bad_css.update({".chapter-title", ".chapter-start-mark", ".chapter-end-mark", 'div[model_target_name="report"]'})

    def download_chapter(self, chapter: Chapter):
        soup = self.scraper.get_soup(chapter["url"])
        marker = soup.select_one(".chapter-start-mark")
        if marker:
            chapter.body = self.cleaner.extract_contents(marker.parent)
