# -*- coding: utf-8 -*-
"""Crawler for NovelHi."""

import codecs
import html as html_module
import logging
from urllib.parse import parse_qs, quote, urlencode, urlparse

from lncrawl.core import Chapter, LegacyCrawler, Volume

logger = logging.getLogger(__name__)
search_url = "%s/book/searchByPageInShelf?curr=1&limit=10&keyword=%s"
fetch_chapter_list_url = "%s/book/queryIndexList?bookId=%s&curr=1&limit=50000"
fetch_chapter_body_url = "%s/book/queryBookIndexContent?bookId=%s&bookIndexId=%s&autoUnlock=false"


class NovelHiCrawler(LegacyCrawler):
    base_url = ["https://novelhi.com/"]

    def search_novel(self, query):
        data = self.get_json(search_url % (self.scraper.origin, quote(query)))
        return [
            {
                "title": item["bookName"],
                "url": f"{self.scraper.origin}/s/{item['simpleName']}",
                "info": f"Latest: {item['lastIndexName']} ({item['lastIndexUpdateTime']})",
            }
            for item in data.get("data", {}).get("list", [])
        ]

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        book_id = soup.select_one("input#bookId")
        if not book_id:
            raise RuntimeError("NovelHi book ID was not found")
        self.novel_id = book_id["value"]

        title = soup.select_one("h1")
        if title:
            self.novel_title = title.get_text(" ", strip=True)
        else:
            image = soup.select_one("a.book_cover img.cover")
            if image:
                self.novel_title = image.get("alt", "").replace(" | Novel Cover", "").strip()

        image = soup.select_one("a.book_cover img.cover")
        if image and image.get("src"):
            self.novel_cover = self.absolute_url(image["src"])

        synopsis = soup.select_one(".intro_txt, .intro, .book_intro, .book_info .intro")
        if synopsis:
            self.novel_synopsis = self.cleaner.extract_contents(synopsis)
        for span in soup.select(".book_info ul.list span.item"):
            if span.get_text(" ", strip=True).startswith("Author"):
                author = span.find_next()
                if author:
                    self.novel_author = author.get_text(" ", strip=True)
                    break

        data = self.get_json(fetch_chapter_list_url % (self.scraper.origin, self.novel_id))
        items = data.get("data", {}).get("list", [])
        for item in reversed(items):
            chapter_id = len(self.chapters) + 1
            volume_id = len(self.chapters) // 100 + 1
            if len(self.volumes) < volume_id:
                self.volumes.append(Volume(id=volume_id))
            index_num = item.get("indexNum", chapter_id)
            index_id = (
                item.get("id")
                or item.get("indexId")
                or item.get("bookIndexId")
                or index_num
            )
            chapter_url = f"{self.novel_url.rstrip('/')}/{index_num}?{urlencode({'bookIndexId': index_id})}"
            self.chapters.append(
                Chapter(
                    id=chapter_id,
                    volume=volume_id,
                    title=item.get("indexName", f"Chapter {chapter_id}"),
                    url=chapter_url,
                )
            )

    def download_chapter_body(self, chapter):
        # NovelHi serves a page shell first, then loads the real content with
        # a per-page token. Reproduce that request instead of scraping the
        # temporary loading placeholder.
        parsed = urlparse(chapter["url"])
        chapter_url = parsed._replace(query="").geturl()
        page = self.get_soup(chapter_url)
        path = page.select_one("#chapterContentPath")
        token = page.select_one("#chapterContentToken")
        if not path or not token:
            return ""

        content_url = self.absolute_url(
            f"{path.get('value', '')}?{urlencode({'token': token.get('value', '')})}"
        )
        if not path.get("value") or not token.get("value"):
            return ""
        response = self.get_response(
            content_url,
            headers={
                "Referer": chapter_url,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        data = response.json()
        if data.get("code") not in (200, "200"):
            return ""
        payload = data.get("data", {})
        html = payload.get("content", "") if isinstance(payload, dict) else payload
        if not html:
            return ""

        soup = self.scraper.make_soup(html)
        sent_nodes = soup.select("sent")
        if sent_nodes:
            paragraphs = []
            for sent in sent_nodes:
                text = codecs.decode(sent.get_text(" ", strip=True), "rot_13")
                if text:
                    paragraphs.append(f"<p>{html_module.escape(text)}</p>")
            return "".join(paragraphs)

        body = soup.select_one("#showReading") or soup
        for node in body.select("script, style, img"):
            node.decompose()
        return self.cleaner.extract_contents(body)



