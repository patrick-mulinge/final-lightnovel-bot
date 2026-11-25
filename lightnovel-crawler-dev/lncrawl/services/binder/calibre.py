from functools import cache
import logging
import os
from pathlib import Path
import shlex
import shutil
import re
import signal as signal_mod
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from threading import Event
from typing import Callable, Dict, List, Optional, Tuple

from ...assets.images import default_cover_image
from ...context import ctx
from ...dao import Artifact, LanguageCode, Novel, OutputFormat
from ...exceptions import AbortedException, ServerErrors

logger = logging.getLogger(__name__)

__is_available = None
__api_timeout = 600
__convert_timeout = 2 * 60 * 60  # kill local ebook-convert after 2 hours
__heartbeat_secs = 60  # how often to print a progress line to the container logs
__poll_secs = 5  # how often the log file is read and listeners are notified
__eta_window_secs = 600  # ETA is based on % progress made in the last 10 minutes
# A PDF render that makes no progress for this long is treated as stuck on one
# chapter (normal chapters take ~1-2 s; Calibre itself only gives up after ~13 min).
__stall_secs = int(os.environ.get("CALIBRE_STALL_SECS", "300"))
__max_skipped_chapters = 15  # give up if more than this many chapters need skipping
# After the first stuck chapter, strip emoji/symbol characters from every chapter
# of the temporary copy that is retried (the emoji font is Calibre's known cause).
__sanitize_all_on_stall = True

# Optional per-conversion progress listeners, keyed by the cancel `signal` Event
# that the caller passes to make_artifact(). The bot registers one so it can show
# live percent / elapsed / ETA in Telegram. Called as listener(info: dict).
progress_listeners: dict = {}
__progress_re = re.compile(r"^\s*(\d{1,3})%\s*(.*)$")
# PDF output renders chapter by chapter: "Rendered: OEBPS/chapter_00459.xhtml in 1.2 seconds (42/508)"
__render_re = re.compile(r"Rendered:\s*(\S+).*\((\d+)/(\d+)\)\s*$")
# "Rendering of clbr://internal.invalid/book/OEBPS/chapter_00910.xhtml not complete after 768 seconds, aborting."
__render_fail_re = re.compile(
    r"Rendering of clbr://[^/\s]+/book/(\S+?\.xhtml) not complete", re.I
)
__chapter_file_re = re.compile(r"^OEBPS/chapter_(\d+)\.xhtml$")

# Optional per-conversion reports, keyed by the cancel `signal` Event. After a
# conversion that had to skip chapters, convert_reports[signal] holds
# {"placeholder": [serials], "cleaned": [serials], "emoji_removed": bool}.
convert_reports: dict = {}


def __fmt_secs(sec: float) -> str:
    sec = int(max(sec, 0))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def __emit(msg: str) -> None:
    # print (not logger) so it always reaches `docker logs` / `podman logs`
    print(f"[CALIBRE] {msg}", file=sys.stderr, flush=True)

# Filesystem-path flags rejected by the ebook-convert-api service.
_BLOCKED_FLAGS = {
    "cover",
    "debug-pipeline",
    "extract-to",
    "transform-css-rules",
}

# An ordered conversion option: (flag, value). A `None` value means a boolean flag.
Option = Tuple[str, Optional[str]]


@cache
def __calibre_exe_path() -> Optional[Path]:
    command = ctx.config.calibre.command
    exe_path = shutil.which(command)
    if not exe_path:
        return None
    path = Path(exe_path)
    if path.is_file():
        return path
    return path


def is_calibre_available() -> bool:
    """Whether any conversion backend (API service or local Calibre) is usable."""
    if ctx.config.calibre.api_enabled:
        return True
    return __calibre_exe_path() is not None


def __parse_option_string(value: str) -> List[Option]:
    """Parse a command-line option string into ordered (flag, value) pairs."""
    tokens = shlex.split(value)
    options: List[Option] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token.startswith("--"):
            continue
        flag = token[2:]
        if index < len(tokens) and not tokens[index].startswith("-"):
            options.append((flag, tokens[index]))
            index += 1
        else:
            options.append((flag, None))
    return options


def __build_options(
    artifact: Artifact, novel: Novel, title: Optional[str] = None
) -> List[Option]:
    """Build the ordered conversion options shared by both backends."""
    options: List[Option] = []
    options += [
        ("enable-heuristics", None),
        ("no-chapters-in-toc", None),
        ("unsmarten-punctuation", None),
        ("disable-renumber-headings", None),
    ]
    options += [
        ("title", title or novel.title),
        ("authors", novel.authors),
        ("tags", ",".join(novel.tags)),
        ("series", novel.title),
        ("publisher", novel.url),
        ("book-producer", "Lightnovel Crawler"),
    ]
    language = artifact.language or novel.language
    if language:
        options += [("language", language)]
    if artifact.format == OutputFormat.pdf:
        options += [
            ("paper-size", "letter"),
            ("pdf-page-numbers", None),
            ("pdf-hyphenate", None),
            (
                "pdf-header-template",
                '<p style="text-align:center; color:#555; font-size:0.9em">'
                "⦗ _TITLE_ &mdash; _SECTION_ ⦘</p>",
            ),
        ]
    return options


def __to_cli_args(options: List[Option]) -> List[str]:
    """Render options as `ebook-convert` command-line arguments."""
    args: List[str] = []
    for flag, value in options:
        args.append(f"--{flag}")
        if value is not None:
            args.append(value)
    return args


class _RenderFailure(Exception):
    """Calibre could not render one chapter (it hung, or reported it could not)."""

    def __init__(self, href: Optional[str], reason: str, tail: str = ""):
        super().__init__(reason)
        self.href = href
        self.reason = reason
        self.tail = tail


class _Notifier:
    """Hands progress info to the listener on its own thread.

    A slow listener (a database timeout, a Telegram rate limit) must never stall
    the loop that watches Calibre. Only the newest info is kept.
    """

    def __init__(self, listener):
        self._listener = listener
        self._lock = threading.Lock()
        self._pending = None
        self._running = False

    def __call__(self, info: dict) -> None:
        if not self._listener:
            return
        with self._lock:
            self._pending = info
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        while True:
            with self._lock:
                info, self._pending = self._pending, None
                if info is None:
                    self._running = False
                    return
            try:
                self._listener(info)
            except Exception as e:  # never let a UI problem break the conversion
                logger.warning(f"calibre progress listener failed: {e!r}")


def __kill_tree(p: subprocess.Popen) -> None:
    """Kill ebook-convert and the helper processes (Qt WebEngine...) it started."""
    try:
        os.killpg(p.pid, signal_mod.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def __chapter_serial(href: str) -> int:
    m = re.search(r"chapter_(\d+)\.xhtml", href)
    return int(m.group(1)) if m else -1


def __chapter_hrefs(epub_file: Path) -> set:
    try:
        with zipfile.ZipFile(epub_file) as z:
            return {n for n in z.namelist() if __chapter_file_re.match(n)}
    except Exception:
        return set()


def __guess_stalled_chapter(rendered: List[str], hrefs: set) -> Optional[str]:
    """The chapter Calibre was working on when the render counter stopped.

    Calibre renders chapters one at a time in a fixed direction (the log shows
    them counting down), so the stuck one is the next unrendered chapter in
    that direction.
    """
    chapters = [r for r in rendered if r in hrefs]
    if len(chapters) < 2:
        return None
    last, prev = __chapter_serial(chapters[-1]), __chapter_serial(chapters[-2])
    done = set(chapters)
    todo = [h for h in hrefs if h not in done]
    if last < prev:
        cands = [h for h in todo if __chapter_serial(h) < last]
        return max(cands, key=__chapter_serial) if cands else None
    cands = [h for h in todo if __chapter_serial(h) > last]
    return min(cands, key=__chapter_serial) if cands else None


# Characters that make Qt WebEngine hang on some systems (colour emoji fonts etc).
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FFFF"  # emoji, pictographs, symbols
    "\U000E0000-\U000E007F"  # tag characters
    "\uFE0E\uFE0F\u200D\u20E3"  # variation selectors, ZWJ, keycap
    "\u2300-\u23FF"  # misc technical (watch, hourglass...)
    "\u2B00-\u2BFF"  # misc symbols and arrows (star, squares...)
    "]"
    "|(?![\u2605\u2606\u2726\u2727])[\u2600-\u27BF]"  # misc symbols + dingbats, but keep ★☆✦✧
)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_SVG_RE = re.compile(r"<svg\b.*?</svg>", re.I | re.S)
_STYLE_ATTR_RE = re.compile(r'\sstyle="[^"]*"', re.I)


def __sanitize_chapter(data: bytes, deep: bool) -> bytes:
    text = data.decode("utf-8", errors="replace")
    text = _EMOJI_RE.sub("", text)
    if deep:
        text = _SVG_RE.sub("", text)
        text = _IMG_RE.sub("", text)
        text = _STYLE_ATTR_RE.sub("", text)
    return text.encode("utf-8")


def __placeholder_chapter(data: bytes) -> bytes:
    from .epub import _xhtml, _xml

    text = data.decode("utf-8", errors="replace")
    h1 = re.search(r"<h1>(.*?)</h1>", text, re.S)
    h4 = re.search(r"<h4[^>]*>(.*?)</h4>", text, re.S)
    title_html = h1.group(1) if h1 else ""
    plain_title = re.sub(r"<[^>]+>", "", title_html).strip() or "Chapter"
    body = (
        '<div id="chapter">'
        f'<h4 style="opacity: 0.75" aria-hidden="true">{h4.group(1) if h4 else ""}</h4>'
        f"<h1>{title_html}</h1>"
        "<p><em>This chapter could not be rendered in this file. "
        "The full text is in the EPUB.</em></p>"
        "</div>"
    )
    return _xhtml(_xml(plain_title), body).encode("utf-8")


def __patch_epub(src: Path, dst: Path, sanitize_all: bool, levels: Dict[str, int]) -> None:
    """Write a copy of `src` where stuck chapters are cleaned (level 1) or replaced
    by a short placeholder (level 2). With `sanitize_all`, emoji are also removed
    from every other chapter."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
        dst, "w", compression=zipfile.ZIP_DEFLATED
    ) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if __chapter_file_re.match(info.filename):
                level = levels.get(info.filename, 0)
                if level >= 2:
                    data = __placeholder_chapter(data)
                elif level == 1:
                    data = __sanitize_chapter(data, deep=True)
                elif sanitize_all:
                    data = __sanitize_chapter(data, deep=False)
            zout.writestr(
                info.filename,
                data,
                compress_type=(
                    zipfile.ZIP_STORED
                    if info.filename == "mimetype"
                    else zipfile.ZIP_DEFLATED
                ),
            )


def __ebook_convert(
    epub_file: Path,
    tmp_file: Path,
    cli_args: list,
    signal: Event,
    extra: Optional[dict] = None,
    chapter_hrefs: Optional[set] = None,
) -> None:
    """
    Calls `ebook-convert` with given args
    Visit https://manual.calibre-ebook.com/generated/en/ebook-convert.html for argument list.

    Raises _RenderFailure when one chapter hangs or cannot be rendered (the caller
    may retry without it), or a ServerErrors exception for any other failure.
    `extra` carries part/attempt info that is added to every progress report.
    """
    exe_path = __calibre_exe_path()
    if not exe_path:
        raise ServerErrors.calibre_exe_not_found.with_extra(exe_path)

    extra = extra if extra is not None else {}
    chapter_hrefs = chapter_hrefs or set()
    cmd = [exe_path.as_posix(), epub_file, tmp_file] + cli_args
    cmd = list(map(str, cmd))
    logger.info(shlex.join(cmd))  # type: ignore
    # Write output to a file instead of PIPEs: unread pipes fill up (~64KB) and
    # block ebook-convert forever on large books.
    log_file = tmp_file.with_name(tmp_file.name + ".convert.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    overall_start = extra.get("overall_start", started)
    name = tmp_file.name
    offset, pct, stage, last_line = 0, 0, "starting", ""
    sub = {"done": 0, "total": 0}  # chapter-render counter, the only reliable fine-grained progress
    sub_history = []  # (time, rendered chapters) samples used for the ETA
    rendered_names: List[str] = []
    phase = {"render_start": None, "render_end": None, "last_render": None}  # monotonic times
    next_beat = started + __heartbeat_secs
    notifier = _Notifier(progress_listeners.get(signal))

    def poll_log() -> None:
        nonlocal offset, pct, stage, last_line
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                chunk = f.read()
                offset = f.tell()
        except OSError:
            return
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            last_line = line[:160]
            r = __render_re.search(line)
            if r:
                rendered_names.append(r.group(1))
                sub["done"], sub["total"] = int(r.group(2)), int(r.group(3))
                now_t = time.monotonic()
                sub_history.append((now_t, sub["done"]))
                phase["last_render"] = now_t
                if phase["render_start"] is None:
                    phase["render_start"] = now_t
                continue
            m = __progress_re.match(line)
            if m and int(m.group(1)) <= 100:
                new_pct, new_stage = int(m.group(1)), m.group(2).strip()
                if new_pct != pct or new_stage != stage:
                    pct, stage = new_pct, new_stage
                    # "90% Rendered all HTML as PDF" marks the end of rendering.
                    if pct >= 90 and phase["render_end"] is None and phase["render_start"]:
                        phase["render_end"] = time.monotonic()
                    __emit(
                        f"{name}: {pct}% {stage} "
                        f"(elapsed {__fmt_secs(time.monotonic() - started)})"
                    )

    def estimate_eta(now: float) -> Optional[float]:
        """Seconds left for the current step, or None if unknown.

        Calibre's own percentages follow its stages, not the clock, and sit at
        one value for most of a PDF conversion, so they are useless for an ETA.
        The "(done/total)" render counter is steady, so the ETA comes from the
        render rate over the last few minutes. Steps after rendering (merging
        pages, page numbers) are not included.
        """
        done, total = sub["done"], sub["total"]
        if phase["render_end"] is not None:
            # Finishing steps (links, headers/footers, metadata). On a 500-chapter test
            # they took about 0.3x as long as rendering did; use that as a rough guide.
            render_secs = phase["render_end"] - phase["render_start"]
            left = 0.3 * render_secs - (now - phase["render_end"])
            return left if left > 5 else None
        if not total or done >= total:
            return None
        recent = [(t, v) for t, v in sub_history if now - t <= __eta_window_secs]
        if len(recent) < 2:
            return None
        (t0, v0), (t1, v1) = recent[0], recent[-1]
        if v1 <= v0 or t1 - t0 < 5:
            return None
        return (total - done) / ((v1 - v0) / (t1 - t0))

    def overall_eta(now: float, eta_now: Optional[float]) -> Optional[float]:
        """Time left for all remaining parts, once at least one part has finished."""
        avg, parts, part = extra.get("avg_part_secs"), extra.get("parts"), extra.get("part")
        if not avg or not parts or not part:
            return None
        part_elapsed = now - extra.get("part_start", started)
        left_here = max(avg - part_elapsed, eta_now or 0, 0)
        return left_here + (parts - part) * avg

    def notify(now: float) -> None:
        try:
            log_kb = log_file.stat().st_size // 1024
        except OSError:
            log_kb = 0
        eta_now = estimate_eta(now)
        info = {
            "pct": pct,
            "stage": stage,
            "elapsed": now - overall_start,
            "attempt_elapsed": now - started,
            "eta": eta_now,
            "overall_eta": overall_eta(now, eta_now),
            "last_line": last_line,
            "sub_done": sub["done"],
            "sub_total": sub["total"],
            "log_kb": log_kb,
            "name": name,
        }
        info.update({k: v for k, v in extra.items() if k not in ("overall_start", "part_start")})
        notifier(info)

    with open(log_file, "w", encoding="utf-8", errors="replace") as out:
        with subprocess.Popen(
            cmd,
            stdout=out,
            stderr=subprocess.STDOUT,
            text=True,
            # Own process group, so a stuck render can be killed together
            # with the Qt WebEngine helper processes it started.
            start_new_session=True,
        ) as p:
            notify(time.monotonic())
            next_poll = started + __poll_secs
            while p.poll() is None:
                if signal.is_set():
                    __kill_tree(p)
                    raise AbortedException()
                now = time.monotonic()
                elapsed = now - started
                if elapsed > __convert_timeout:
                    __kill_tree(p)
                    raise ServerErrors.ebook_convert_error.with_extra(
                        f"ebook-convert timed out after {__convert_timeout}s"
                    )
                if now >= next_poll:
                    next_poll = now + __poll_secs
                    poll_log()
                    notify(now)
                    # Rendering started but no chapter finished for a long time:
                    # one chapter is stuck. Don't wait for Calibre's own ~13 min abort.
                    if (
                        phase["last_render"] is not None
                        and phase["render_end"] is None
                        and now - phase["last_render"] > __stall_secs
                    ):
                        poll_log()
                        __kill_tree(p)
                        href = __guess_stalled_chapter(rendered_names, chapter_hrefs)
                        raise _RenderFailure(
                            href,
                            f"render made no progress for {__stall_secs}s "
                            f"after {sub['done']}/{sub['total']}",
                            last_line,
                        )
                if now >= next_beat:
                    next_beat = now + __heartbeat_secs
                    eta = estimate_eta(now)
                    eta_txt = f", render ETA {__fmt_secs(eta)}" if eta else ""
                    if sub["total"]:
                        eta_txt = f", rendered {sub['done']}/{sub['total']}" + eta_txt
                    try:
                        size_kb = log_file.stat().st_size // 1024
                    except OSError:
                        size_kb = 0
                    __emit(
                        f"{name}: still running, {pct}% [{stage}], "
                        f"elapsed {__fmt_secs(elapsed)}{eta_txt}, log {size_kb}KB | "
                        f"last: {last_line}"
                    )
                signal.wait(0.1)
    __kill_tree(p)  # stray helper processes of a finished (or aborted) run
    poll_log()
    __emit(
        f"{name}: finished with exit code {p.returncode} "
        f"in {__fmt_secs(time.monotonic() - started)}"
    )
    if p.returncode != 0:
        try:
            tail = log_file.read_text(errors="replace")[-2000:]
        except Exception:
            tail = ""
        m = __render_fail_re.search(tail)
        if m:
            raise _RenderFailure(m.group(1), "Calibre gave up rendering a chapter", tail)
        raise ServerErrors.ebook_convert_error.with_extra(
            tail or f"exit code {p.returncode}"
        )
    log_file.unlink(missing_ok=True)


def __convert_local(
    epub_file: Path,
    tmp_file: Path,
    cli_args: list,
    signal: Event,
    extra: Optional[dict] = None,
) -> dict:
    """Run ebook-convert; when one chapter hangs, retry without it instead of failing.

    1st stuck chapter: emoji/symbols are stripped from the retried copy (every
    chapter), and the stuck chapter also loses images and inline styles.
    Same chapter stuck again: it is replaced by a one-line placeholder.
    Returns {"placeholder": [serials], "cleaned": [serials], "emoji_removed": bool}.
    """
    extra = extra if extra is not None else {}
    hrefs = __chapter_hrefs(epub_file)
    levels: Dict[str, int] = {}
    sanitize_all = False
    current = epub_file
    patched = tmp_file.with_name(tmp_file.stem + ".patched.epub")
    try:
        while True:
            tmp_file.unlink(missing_ok=True)
            try:
                __ebook_convert(current, tmp_file, cli_args, signal, extra, hrefs)
                break
            except _RenderFailure as f:
                if not f.href or f.href not in hrefs:
                    raise ServerErrors.ebook_convert_error.with_extra(
                        f.tail or f.reason
                    ) from f
                level = levels.get(f.href, 0) + 1
                if level > 2 or (f.href not in levels and len(levels) >= __max_skipped_chapters):
                    raise ServerErrors.ebook_convert_error.with_extra(
                        f"Chapter {__chapter_serial(f.href)} cannot be rendered even "
                        f"after cleaning it. {f.reason}"
                    ) from f
                levels[f.href] = level
                sanitize_all = sanitize_all or __sanitize_all_on_stall
                action = "cleaned" if level == 1 else "replaced by a placeholder"
                __emit(
                    f"{tmp_file.name}: {f.reason}; chapter {__chapter_serial(f.href)} "
                    f"will be {action} and the conversion restarted"
                )
                __patch_epub(epub_file, patched, sanitize_all, levels)
                current = patched
                extra["attempt"] = extra.get("attempt", 1) + 1
                extra["skipped"] = sorted(
                    __chapter_serial(h) for h, lv in levels.items() if lv >= 2
                )
                extra["cleaned"] = sorted(
                    __chapter_serial(h) for h, lv in levels.items() if lv == 1
                )
    finally:
        patched.unlink(missing_ok=True)
    return {
        "placeholder": sorted(__chapter_serial(h) for h, lv in levels.items() if lv >= 2),
        "cleaned": sorted(__chapter_serial(h) for h, lv in levels.items() if lv == 1),
        "emoji_removed": bool(levels) and sanitize_all,
    }


def __to_form_fields(options: List[Option]) -> dict:
    """Render options as ebook-convert-api form fields."""
    data: dict = {}
    for flag, value in options:
        if flag in _BLOCKED_FLAGS:
            continue
        name = flag.replace("-", "_")
        if value is None:
            data[name] = "true"
        else:
            data[name] = value
    return data


def __convert_via_api(
    epub_file: Path,
    tmp_file: Path,
    cover_file: Path,
    fields: dict,
    signal: Event,
) -> None:
    """Convert the EPUB using the remote ebook-convert-api service."""
    url = f"{ctx.config.calibre.api_url}/convert"
    with (
        open(epub_file, "rb") as fp,
        open(cover_file, "rb") as cp,
    ):
        resp = ctx.http.post(
            url,
            signal=signal,
            data=fields,
            files={
                "file": (epub_file.name, fp),
                "cover": (cover_file.name, cp),
            },
            timeout=__api_timeout,
        )
        if not resp.ok:
            # The body carries the API's reason (e.g. which form field failed a
            # 422 validation) — surface it instead of a bare status code.
            logger.warning(f"calibre-api {resp.status_code} for {url}: {resp.text}")
        resp.raise_for_status()
        tmp_file.write_bytes(resp.content)


def __convert_one(
    epub_file: Path,
    tmp_file: Path,
    artifact: Artifact,
    novel: Novel,
    signal: Event,
    extra: Optional[dict] = None,
    title: Optional[str] = None,
) -> dict:
    """Convert one EPUB to `tmp_file` with the API service and/or local Calibre."""
    cfg = ctx.config.calibre
    options = __build_options(artifact, novel, title=title)
    if novel.cover_available:
        cover_file = ctx.files.resolve(novel.cover_file)
    else:
        cover_file = default_cover_image()

    report: dict = {"placeholder": [], "cleaned": [], "emoji_removed": False}
    converted = False
    if cfg.api_enabled:
        try:
            fields = __to_form_fields(options)
            fields["output_format"] = str(artifact.format)
            __convert_via_api(epub_file, tmp_file, cover_file, fields, signal)
            converted = tmp_file.is_file()
        except AbortedException:
            raise
        except Exception as e:
            logger.warning(f"calibre-api conversion failed. {e!r}", exc_info=ctx.logger.is_debug)
            if not cfg.api_fallback_to_local:
                raise ServerErrors.ebook_convert_error from e

    if not converted:
        args = __to_cli_args(options)
        args += ["--cover", cover_file.as_posix(), "--verbose"]
        report = __convert_local(epub_file, tmp_file, args, signal, extra)
        converted = tmp_file.is_file()

    if not converted:
        raise ServerErrors.failed_creating_artifact
    return report


def convert_epub(
    working_dir: Path,
    artifact: Artifact,
    signal=Event(),
    epub: Optional[Artifact] = None,
) -> None:
    out_file = ctx.files.resolve(artifact.output_file)
    tmp_file = working_dir / out_file.name
    if not epub:
        raise ServerErrors.no_epub_file

    language = LanguageCode(artifact.language) if artifact.language else None
    novel = ctx.novels.get(artifact.novel_id, language)
    epub_file = ctx.files.resolve(epub.output_file)

    if not is_calibre_available():
        raise ServerErrors.calibre_exe_not_found

    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f'Converting "{epub_file.name}" to "{out_file.name}"')

    wants_report = signal in progress_listeners  # only the bot reads convert_reports
    try:
        report = __convert_one(epub_file, tmp_file, artifact, novel, signal)
    finally:
        progress_listeners.pop(signal, None)
    if wants_report and (report.get("placeholder") or report.get("cleaned")):
        convert_reports[signal] = report

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.unlink(True)
    shutil.move(str(tmp_file), str(out_file))
    logger.info("Created: %s", out_file.name)


def count_epub_chapters(epub: Artifact) -> int:
    """Chapters inside a finished EPUB artifact."""
    from .split import count_chapters

    return count_chapters(ctx.files.resolve(epub.output_file))


def convert_epub_parts(
    epub: Artifact,
    format: OutputFormat,
    size: int,
    out_dir: Path,
    file_stem: str,
    signal=Event(),
    on_part_ready: Optional[Callable[[dict], None]] = None,
) -> List[dict]:
    """
    Convert a big EPUB in parts of about `size` chapters each.

    Every part is converted on its own, so memory stays small and a part that
    fails does not cost the parts that worked. Part files are named
    "<file_stem> c<first>-<last>.<format>" and written to `out_dir`.
    `on_part_ready(result)` is called as soon as each part finishes (or fails),
    with {"index", "total", "first", "last", "path", "name", "skipped",
    "cleaned", "emoji_removed", "error"}.
    Raises only when cancelled or when every part failed.
    """
    from .split import EpubSplitter

    if not is_calibre_available():
        raise ServerErrors.calibre_exe_not_found
    epub_file = ctx.files.resolve(epub.output_file)
    language = LanguageCode(epub.language) if epub.language else None
    novel = ctx.novels.get(epub.novel_id, language)

    splitter = EpubSplitter(epub_file, novel, epub.language)
    plans = splitter.plan(size)
    total = len(plans)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="parts_work_", dir=out_dir))
    overall_start = time.monotonic()
    durations: List[float] = []
    results: List[dict] = []
    __emit(f"splitting {epub_file.name} into {total} parts of about {size} chapters")

    try:
        for plan in plans:
            if signal.is_set():
                raise AbortedException()
            name = f"{file_stem} c{plan.first}-{plan.last}.{format}"
            part_epub = work / f"part_{plan.index:03}.epub"
            tmp_out = work / name
            part_start = time.monotonic()
            result = {
                "index": plan.index,
                "total": total,
                "first": plan.first,
                "last": plan.last,
                "name": name,
                "path": None,
                "skipped": [],
                "cleaned": [],
                "emoji_removed": False,
                "error": None,
            }
            extra = {
                "part": plan.index,
                "parts": total,
                "first": plan.first,
                "last": plan.last,
                "attempt": 1,
                "skipped": [],
                "cleaned": [],
                "overall_start": overall_start,
                "part_start": part_start,
                "avg_part_secs": (sum(durations) / len(durations)) if durations else None,
            }
            try:
                splitter.write(plan, part_epub)
                part_artifact = Artifact(
                    novel_id=novel.id,
                    format=format,
                    language=epub.language,
                    file_name=name,
                )
                report = __convert_one(
                    part_epub,
                    tmp_out,
                    part_artifact,
                    novel,
                    signal,
                    extra,
                    title=f"{novel.title} (c{plan.first}-{plan.last})",
                )
                final = out_dir / name
                final.unlink(missing_ok=True)
                shutil.move(str(tmp_out), str(final))
                result.update(
                    path=str(final),
                    skipped=report.get("placeholder", []),
                    cleaned=report.get("cleaned", []),
                    emoji_removed=bool(report.get("emoji_removed")),
                )
                durations.append(time.monotonic() - part_start)
            except AbortedException:
                raise
            except Exception as e:
                if signal.is_set():
                    raise AbortedException() from e
                logger.warning(f"part {plan.index}/{total} failed: {e!r}")
                result["error"] = str(e)[-600:] or type(e).__name__
                __emit(f"part {plan.index}/{total} (c{plan.first}-{plan.last}) failed: {result['error'][:200]}")
            finally:
                part_epub.unlink(missing_ok=True)
                tmp_out.unlink(missing_ok=True)
            results.append(result)
            if on_part_ready:
                try:
                    on_part_ready(result)
                except Exception as e:
                    logger.warning(f"on_part_ready failed: {e!r}")
    finally:
        progress_listeners.pop(signal, None)
        shutil.rmtree(work, ignore_errors=True)

    if results and all(r["error"] for r in results):
        raise ServerErrors.ebook_convert_error.with_extra(
            "Every part failed. Last error: " + str(results[-1]["error"])
        )
    return results
