# -*- coding: utf-8 -*-
"""Crawler for NovelCEO novel and chapter pages."""

import logging
import re
from urllib.parse import urljoin, urlparse

from lncrawl.core import Chapter, LegacyCrawler, Volume


logger = logging.getLogger(__name__)


class NovelCeoCrawler(LegacyCrawler):
    base_url = ["https://novelceo.com/"]

    SINGLES_UNTIL = 30      # chapter-1 .. chapter-30 are single pages
    RANGE_SIZE = 5          # then chapter-31-35, chapter-36-40, ...
    RANGES_UNTIL = 1900     # last merged range ends here; singles after that

    def _toc_links(self, soup, novel_path):
        entries = {}  # (start, end) -> (title, url)
        for link in soup.select("a[href*='/chapter-']"):
            href = link.get("href", "")
            if urlparse(href).path.rsplit("/chapter-", 1)[0] != novel_path:
                continue  # link to another novel (recommendations etc.)
            match = re.search(r"/chapter-(\d+)(?:-(\d+))?/?$", href)
            if not match:
                continue
            start = int(match.group(1))
            end = int(match.group(2) or start)
            label = "Chapter %d" % start if start == end else "Chapter %d-%d" % (start, end)
            entries.setdefault(
                (start, end),
                (link.get_text(" ", strip=True) or label, urljoin(self.scraper.origin, href)),
            )
        if not entries:
            # not an HTML fragment (e.g. JSON): pull chapter URLs out of the raw text
            raw = str(soup).replace("\\/", "/")
            for href in re.findall(r"""["'(]([^"'()\s]*?/chapter-\d+(?:-\d+)?)/?["')]""", raw):
                if urlparse(href).path.rsplit("/chapter-", 1)[0] != novel_path:
                    continue
                match = re.search(r"/chapter-(\d+)(?:-(\d+))?$", href)
                start = int(match.group(1))
                end = int(match.group(2) or start)
                label = "Chapter %d" % start if start == end else "Chapter %d-%d" % (start, end)
                entries.setdefault((start, end), (label, urljoin(self.scraper.origin, href)))
        return entries

    def _pattern_entries(self, novel_path, total):
        base = urljoin(self.scraper.origin, novel_path)
        entries = {}
        for n in range(1, min(self.SINGLES_UNTIL, total) + 1):
            entries[(n, n)] = ("Chapter %d" % n, "%s/chapter-%d" % (base, n))
        step = self.RANGE_SIZE
        for start in range(self.SINGLES_UNTIL + 1, min(self.RANGES_UNTIL, total), step):
            end = start + step - 1
            entries[(start, end)] = ("Chapter %d-%d" % (start, end), "%s/chapter-%d-%d" % (base, start, end))
        for n in range(self.RANGES_UNTIL + 1, total + 1):
            entries[(n, n)] = ("Chapter %d" % n, "%s/chapter-%d" % (base, n))
        return entries

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)
        title = soup.select_one("h1 a[itemprop='item'] span[itemprop='name'], h3.title")
        self.novel_title = title.get_text(" ", strip=True) if title else ""
        author = soup.select_one("[itemprop='author'] [itemprop='name'], .info a[href*='/author/']")
        self.novel_author = author.get("content", "").strip() if author and author.has_attr("content") else (author.get_text(" ", strip=True) if author else "")
        description = soup.select_one("#novel-description-content, .desc-text")
        self.novel_synopsis = description.get_text(" ", strip=True) if description else ""
        cover = soup.select_one("meta[property='og:image'], meta[itemprop='image']")
        if cover and cover.get("content"):
            self.novel_cover = urljoin(self.scraper.origin, cover["content"])

        self.volumes.append(Volume(id=1, title="Volume 1"))

        # NovelCEO serves chapters 1-30 as single pages (chapter-N) but merges
        # later ones into range pages (chapter-31-35, chapter-36-40, ...), and
        # a few numbers do not exist. Generating chapter-1..chapter-N URLs 404s
        # from chapter 31 on. The site also builds its chapter list with JS, so
        # a plain fetch only sees a couple of links. Strategies, in order:
        #   1. links present in the fetched page
        #   2. the site's ajax chapter-archive endpoint
        #   3. the known URL pattern (logged as a warning)
        novel_path = urlparse(self.novel_url).path.rstrip("/")
        entries = self._toc_links(soup, novel_path)

        latest = soup.select_one("meta[property='og:novel:lastest_chapter_url']")
        latest_match = re.search(r"chapter-(\d+)", latest.get("content", "") if latest else "", re.I)
        total = int(latest_match.group(1)) if latest_match else 0

        def covered():
            return sum(end - start + 1 for start, end in entries)

        if total and covered() < 0.9 * total:
            holder = soup.select_one("[data-novel-id]")
            novel_id = holder.get("data-novel-id") if holder else ""
            if novel_id:
                try:
                    ajax = self.get_soup(
                        urljoin(self.scraper.origin, "ajax/chapter-archive?novelId=%s" % novel_id),
                        headers={
                            "x-requested-with": "XMLHttpRequest",
                            "referer": self.novel_url,
                            "accept": "*/*",
                        },
                    )
                    for key, val in self._toc_links(ajax, novel_path).items():
                        entries.setdefault(key, val)
                except Exception as exc:
                    logger.warning("NovelCEO ajax chapter-archive failed: %s", exc)

        if total and covered() < 0.9 * total:
            logger.warning(
                "NovelCEO: page lists only %d of %d chapters; falling back to the "
                "known URL pattern (singles to %d, %d-chapter ranges to %d, then "
                "singles). Chapters that do not exist will show up as missing.",
                covered(), total, self.SINGLES_UNTIL, self.RANGE_SIZE, self.RANGES_UNTIL,
            )
            for key, val in self._pattern_entries(novel_path, total).items():
                entries.setdefault(key, val)

        for index, key in enumerate(sorted(entries), start=1):
            title, url = entries[key]
            self.chapters.append(Chapter(id=index, volume=1, title=title, url=url))
        if not self.chapters:
            raise RuntimeError("NovelCEO chapter catalogue was empty")

    def download_chapter_body(self, chapter):
        soup = self.get_soup(chapter.url)
        content = soup.select_one("#chr-content, #chapter-content")
        if not content:
            title = soup.select_one("title")
            logger.warning(
                "NovelCEO: no #chr-content at %s (page title: %r)",
                chapter.url,
                title.get_text(" ", strip=True) if title else None,
            )
            return ""
        # Keep only the story paragraphs: the container also holds ad blocks.
        for child in list(content.children):
            if getattr(child, "name", None) not in ("p", "br"):
                if hasattr(child, "decompose"):
                    child.decompose()
        for p in content.find_all("p"):
            if p.get_text(strip=True).lower() == "table of contents":
                p.decompose()
        return self.cleaner.extract_contents(content)
