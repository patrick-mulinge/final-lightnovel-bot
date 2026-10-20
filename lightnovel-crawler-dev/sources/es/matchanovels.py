"""Crawler for MatchaNovels Spanish translations."""

from lncrawl.core import Chapter, Novel, PageSoup, SoupTemplate, Volume


class MatchaNovelsCrawler(SoupTemplate):
    """Crawl server-rendered MatchaNovels novel and chapter pages."""

    base_url = ["https://matchanovels.com/"]
    language = "es"
    has_mtl = False
    has_manga = False
    can_search = False

    novel_title_selector = "h1"
    chapter_body_selector = 'div[data-mn-texto-capitulo="1"]'

    def parse_chapter_body(self, soup: PageSoup, chapter: Chapter) -> None:
        chapter.body = self.cleaner.extract_contents(soup)

    def parse_toc(self, soup: PageSoup, novel: Novel) -> None:
        """Use MatchaNovels' public API; the page itself renders only 25 rows."""
        slug = novel.url.rstrip("/").rsplit("/", 1)[-1]
        api_url = (
            f"{self.scraper.origin.rstrip('/')}/api/v1/public/novels/"
            f"{slug}/chapters?target_language=es"
        )
        data = self.scraper.get_json(api_url)
        chapters: dict[int, tuple[str, str]] = {}

        for item in data:
            if item.get("is_vip_only"):
                continue

            number = int(float(item["chapter_number"]))
            title = item.get("title", "").strip() or f"Capítulo {number}"
            url = self.absolute_url(f"/novela/{slug}/capitulo/{number}")
            chapters[number] = (title, url)

        novel.volumes = [Volume(id=1)]
        novel.chapters = [
            Chapter(id=number, volume=1, title=title, url=url)
            for number, (title, url) in sorted(chapters.items())
        ]

