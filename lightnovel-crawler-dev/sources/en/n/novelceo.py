# -*- coding: utf-8 -*-
"""Crawler for NovelCEO novel and chapter pages."""

import logging
import re
from urllib.parse import urljoin, urlparse

from lncrawl.core import Chapter, LegacyCrawler, Volume


logger = logging.getLogger(__name__)


class NovelCeoCrawler(LegacyCrawler):
    base_url = ["https://novelceo.com/"]

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
        # some numbers are missing. So the real table of contents must be used;
        # generating chapter-1..chapter-N URLs 404s from chapter 31 onwards.
        novel_path = urlparse(self.novel_url).path.rstrip("/")
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

        latest = soup.select_one("meta[property='og:novel:lastest_chapter_url']")
        latest_match = re.search(r"chapter-(\d+)", latest.get("content", "") if latest else "", re.I)
        total = int(latest_match.group(1)) if latest_match else 0
        covered = sum(end - start + 1 for start, end in entries)
        if total and covered < 0.9 * total:
            # The page did not contain the full table of contents; failing
            # loudly beats queueing thousands of URLs that do not exist.
            raise RuntimeError(
                "NovelCEO table of contents incomplete: %d links cover only "
                "%d of %d chapters" % (len(entries), covered, total)
            )

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
