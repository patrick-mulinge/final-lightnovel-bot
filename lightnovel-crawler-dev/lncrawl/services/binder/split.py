"""
Split a finished EPUB into several smaller EPUBs of N chapters each.

This works directly on the EPUB zip that `make_epub` wrote, so it needs no
database access and the chapters never have to be fetched again. It is used to
convert big books (PDF and the other Calibre formats) in parts: every part is a
valid EPUB with its own cover, intro page, volume pages, table of contents and
only the images its chapters use.
"""

import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import unescape
from zipfile import ZIP_DEFLATED, ZIP_STORED

logger = logging.getLogger(__name__)

_CHAPTER_FILE_RE = re.compile(r"^OEBPS/chapter_(\d+)\.xhtml$")
_NAV_VOLUME_RE = re.compile(r"<li><span>(.*?)</span><ol>(.*?)</ol></li>", re.S)
_NAV_CHAPTER_RE = re.compile(r'<li><a href="(chapter_\d+\.xhtml)">(.*?)</a></li>', re.S)
_SPINE_VOLUME_RE = re.compile(r'<itemref idref="volume_(\d+)"')
_IMAGE_RE = re.compile(r"images/([^\"'\s<>/\\]+?)\.jpg")
_UNESCAPE = {"&quot;": '"', "&apos;": "'"}

# A last part smaller than this share of the part size is merged into the part
# before it, so a 1010-chapter book at size 500 becomes 500 + 510, not 500+500+10.
_MIN_TAIL_SHARE = 0.2


@dataclass
class PartPlan:
    index: int  # 1-based
    total: int
    first: int  # serial of the first chapter in this part
    last: int  # serial of the last chapter in this part
    count: int
    # [(volume, [(href, title), ...]), ...] in reading order
    volumes: list = field(default_factory=list)


def count_chapters(epub_path: Path) -> int:
    """Number of chapter files in an EPUB made by make_epub."""
    with zipfile.ZipFile(epub_path) as z:
        return sum(1 for n in z.namelist() if _CHAPTER_FILE_RE.match(n))


class EpubSplitter:
    def __init__(self, epub_path: Path, novel, language: Optional[str] = None):
        self.epub_path = Path(epub_path)
        self.novel = novel
        self.language = language
        self.volumes: List[Tuple[SimpleNamespace, List[Tuple[str, str]]]] = []
        self.has_cover = False
        self._read_layout()

    # ------------------------------------------------------------------ reading
    def _read_layout(self) -> None:
        with zipfile.ZipFile(self.epub_path) as z:
            names = set(z.namelist())
            nav = z.read("OEBPS/nav.xhtml").decode("utf-8", "replace")
            try:
                opf = z.read("OEBPS/content.opf").decode("utf-8", "replace")
            except KeyError:
                opf = ""
            self.has_cover = (
                "OEBPS/cover.xhtml" in names and "OEBPS/images/cover.jpg" in names
            )

        serials = [int(s) for s in _SPINE_VOLUME_RE.findall(opf)]
        nav_volumes = _NAV_VOLUME_RE.findall(nav)
        volumes = []
        for i, (title, body) in enumerate(nav_volumes):
            chapters = [
                (href, unescape(ch_title, _UNESCAPE))
                for href, ch_title in _NAV_CHAPTER_RE.findall(body)
            ]
            serial = serials[i] if i < len(serials) else i + 1
            volume = SimpleNamespace(title=unescape(title, _UNESCAPE), serial=serial)
            volumes.append((volume, chapters))

        if not volumes:
            # Unexpected layout: fall back to every chapter file in one volume.
            with zipfile.ZipFile(self.epub_path) as z:
                hrefs = sorted(
                    n.split("/", 1)[1] for n in z.namelist() if _CHAPTER_FILE_RE.match(n)
                )
            volumes = [
                (
                    SimpleNamespace(title="Chapters", serial=1),
                    [(h, h) for h in hrefs],
                )
            ]
        self.volumes = volumes

    @staticmethod
    def _serial(href: str) -> int:
        return int(href.removeprefix("chapter_").removesuffix(".xhtml"))

    # ----------------------------------------------------------------- planning
    def plan(self, size: int) -> List[PartPlan]:
        flat = []  # (serial, volume_index, href, title)
        for vi, (_vol, chapters) in enumerate(self.volumes):
            for href, title in chapters:
                flat.append((self._serial(href), vi, href, title))
        flat.sort(key=lambda x: x[0])

        if size <= 0 or len(flat) <= size:
            groups = [flat]
        else:
            groups = [flat[i : i + size] for i in range(0, len(flat), size)]
            if len(groups) > 1 and len(groups[-1]) < size * _MIN_TAIL_SHARE:
                groups[-2].extend(groups.pop())

        plans: List[PartPlan] = []
        total = len(groups)
        for n, group in enumerate(groups, 1):
            by_volume: Dict[int, list] = {}
            for _serial, vi, href, title in group:
                by_volume.setdefault(vi, []).append((href, title))
            volumes = [
                (self.volumes[vi][0], by_volume[vi]) for vi in sorted(by_volume)
            ]
            plans.append(
                PartPlan(
                    index=n,
                    total=total,
                    first=group[0][0],
                    last=group[-1][0],
                    count=len(group),
                    volumes=volumes,
                )
            )
        return plans

    # ------------------------------------------------------------------ writing
    def write(self, plan: PartPlan, dest: Path) -> None:
        # Imported here so this module can be loaded without the whole app.
        from .epub import (
            _content_opf,
            _nav_xhtml,
            _toc_ncx,
            _volume_xhtml,
        )

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.epub_path) as zin, zipfile.ZipFile(
            dest, "w", compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True
        ) as zout:
            names = set(zin.namelist())

            zout.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
            for fixed in ("META-INF/container.xml", "OEBPS/style.css", "OEBPS/intro.xhtml"):
                if fixed in names:
                    zout.writestr(fixed, zin.read(fixed))

            if self.has_cover:
                zout.writestr("OEBPS/cover.xhtml", zin.read("OEBPS/cover.xhtml"))
                zout.writestr("OEBPS/images/cover.jpg", zin.read("OEBPS/images/cover.jpg"))

            image_ids: List[str] = []
            seen_images = set()
            for volume, chapters in plan.volumes:
                zout.writestr(
                    f"OEBPS/volume_{volume.serial:03}.xhtml", _volume_xhtml(volume)
                )
                for href, _title in chapters:
                    data = zin.read(f"OEBPS/{href}")
                    zout.writestr(f"OEBPS/{href}", data)
                    for image_id in _IMAGE_RE.findall(data.decode("utf-8", "replace")):
                        if image_id == "cover" or image_id in seen_images:
                            continue
                        seen_images.add(image_id)
                        if f"OEBPS/images/{image_id}.jpg" in names:
                            image_ids.append(image_id)

            for image_id in image_ids:
                member = f"OEBPS/images/{image_id}.jpg"
                zout.writestr(member, zin.read(member))

            zout.writestr("OEBPS/nav.xhtml", _nav_xhtml(self.novel, plan.volumes))
            zout.writestr("OEBPS/toc.ncx", _toc_ncx(self.novel, plan.volumes))
            zout.writestr(
                "OEBPS/content.opf",
                _content_opf(
                    self.novel,
                    self.language or getattr(self.novel, "language", None),
                    self.has_cover,
                    image_ids,
                    plan.volumes,
                ),
            )
