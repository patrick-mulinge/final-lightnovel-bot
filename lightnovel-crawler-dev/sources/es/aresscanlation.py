# -*- coding: utf-8 -*-
"""Crawler for the Spanish Ares Scanlation Madara site."""

from lncrawl.templates.madara import MadaraTemplate


class AresScanlationCrawler(MadaraTemplate):
    base_url = ["https://aresscanlationovel.com/"]
    language = "es"
    has_mtl = False
    has_manga = False

    def select_chapter_tags(self, tag, novel, volume=None):
        # Keep Madara's built-in Show More/AJAX loading, but only accept links
        # inside real chapter list items. This excludes navigation/related links.
        for chapter_link in super().select_chapter_tags(tag, novel, volume):
            item = chapter_link.find_parent("li", class_="wp-manga-chapter")
            if item:
                yield chapter_link

    def read_novel_info(self):
        super().read_novel_info()
        soup = self.get_soup(self.novel_url)
        synopsis = soup.select_one(
            ".summary_content .description-summary, .summary_content .summary_content"
        )
        if synopsis:
            text = synopsis.get_text(" ", strip=True)
            if text:
                self.novel_synopsis = text


