"""Download chapters and pack them as .cbz files.

CBZ layout produced for every chapter:

    000_cover.<ext>     <- cover image taken from the manga's source page
    001.jpg, 002.jpg..  <- chapter pages, in reading order
    ComicInfo.xml       <- ComicRack-compatible metadata

Files are written to ``<output>/<Manga Title>/<Manga Title> - Chapter NNN.cbz``
via a ``.part`` temp file that is atomically renamed on completion, so an
interrupted run never leaves a corrupt .cbz behind.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from .config import Settings
from .models import ChapterInfo, MangaInfo
from .scraper import MangaReadScraper, ScraperError, parse_chapter_page
from .utils import (
    format_chapter_number,
    guess_extension,
    looks_like_image,
    sanitize_filename,
)

log = logging.getLogger(__name__)


@dataclass
class ChapterResult:
    chapter: ChapterInfo
    ok: bool
    path: Path | None = None
    pages: int = 0
    skipped: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class BatchSummary:
    total: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: list[tuple[ChapterInfo, str]] = field(default_factory=list)
    cancelled: bool = False

    @property
    def done(self) -> int:
        return self.succeeded + self.skipped + len(self.failed)


@dataclass
class DownloadCallbacks:
    """Optional hooks the GUI wires up to update progress bars / log view."""

    on_log: Callable[[str], None] | None = None
    on_chapter_start: Callable[[int, int, ChapterInfo], None] | None = None
    on_page_progress: Callable[[int, int], None] | None = None
    on_chapter_done: Callable[[int, int, ChapterResult], None] | None = None

    def log(self, message: str) -> None:
        log.info("%s", message)
        if self.on_log:
            self.on_log(message)


def build_comic_info(manga: MangaInfo, chapter: ChapterInfo, page_count: int) -> bytes:
    """ComicRack-compatible metadata, nicely escaped by ElementTree."""
    root = ET.Element("ComicInfo")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root.set("xmlns:xsd", "http://www.w3.org/2001/XMLSchema")

    def elem(tag: str, text: str | None) -> None:
        if text:
            node = ET.SubElement(root, tag)
            node.text = str(text)

    number = ""
    if chapter.number is not None:
        number = f"{chapter.number:g}"
    elem("Title", chapter.title)
    elem("Series", manga.title)
    elem("Number", number)
    elem("Summary", (manga.summary or "")[:1000])
    elem("Writer", ", ".join(manga.authors))
    elem("Penciller", ", ".join(manga.artists))
    elem("Genre", ", ".join(manga.genres))
    elem("Web", chapter.url)
    elem("PageCount", str(page_count))
    elem("Manga", "Unknown")
    elem("LanguageISO", "en")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def chapter_cbz_path(manga: MangaInfo, chapter: ChapterInfo, output_dir: Path) -> Path:
    series = sanitize_filename(manga.title, fallback="manga")
    number = format_chapter_number(chapter.number, chapter.url.rstrip("/").rsplit("/", 1)[-1])
    filename = sanitize_filename(f"{series} - Chapter {number}.cbz", fallback=f"chapter-{number}.cbz")
    return output_dir / series / filename


class DownloadManager:
    def __init__(self, settings: Settings, scraper: MangaReadScraper | None = None):
        self.settings = settings
        self.scraper = scraper or MangaReadScraper(delay=settings.delay, timeout=settings.timeout,
                                                    retries=settings.retries)
        self._cover_cache: tuple[bytes, str] | None | bool = None  # bytes+ext, or False when unavailable, or None = not tried

    # ------------------------------------------------------------- cover
    def _get_cover(self, manga: MangaInfo, callbacks: DownloadCallbacks) -> tuple[bytes, str] | None:
        if self._cover_cache is not None:
            return self._cover_cache or None
        candidates = [u for u in (manga.cover_url, manga.cover_thumb_url) if u]
        for url in candidates:
            try:
                data, content_type = self.scraper.fetch_bytes_with_type(url)
                if data and looks_like_image(data):
                    ext = guess_extension(url, content_type, default=".jpg")
                    self._cover_cache = (data, ext)
                    callbacks.log(f"Cover image fetched ({len(data) / 1024:.0f} KB) — it will be the first page of each .cbz")
                    return self._cover_cache
                callbacks.log(f"Cover candidate did not look like an image: {url}")
            except ScraperError as exc:
                callbacks.log(f"Could not download cover from {url}: {exc}")
        callbacks.log("WARNING: no cover image could be downloaded — .cbz files will be built without one")
        self._cover_cache = False
        return None

    # ----------------------------------------------------------- chapters
    def download_chapter(self, manga: MangaInfo, chapter: ChapterInfo, output_dir: Path,
                         cancel_event: threading.Event, callbacks: DownloadCallbacks,
                         index: int, total: int) -> ChapterResult:
        callbacks.on_chapter_start and callbacks.on_chapter_start(index, total, chapter)
        final_path = chapter_cbz_path(manga, chapter, output_dir)

        if self.settings.skip_existing and final_path.is_file() and final_path.stat().st_size > 0:
            callbacks.log(f"Skipping (already exists): {final_path.name}")
            result = ChapterResult(chapter=chapter, ok=True, path=final_path, skipped=True)
            callbacks.on_chapter_done and callbacks.on_chapter_done(index, total, result)
            return result

        # 1. chapter page → image URLs
        try:
            html = self.scraper.fetch_text(chapter.url)
            image_urls, _title = parse_chapter_page(html, chapter.url)
        except ScraperError as exc:
            callbacks.log(f"FAILED {chapter.label}: {exc}")
            result = ChapterResult(chapter=chapter, ok=False, error=str(exc))
            callbacks.on_chapter_done and callbacks.on_chapter_done(index, total, result)
            return result

        callbacks.log(f"{chapter.label}: {len(image_urls)} pages found")

        # 2. cover (cached across the batch)
        cover = self._get_cover(manga, callbacks)

        # 3. download pages
        pages: list[tuple[bytes, str]] = []
        warnings: list[str] = []
        for page_index, image_url in enumerate(image_urls, start=1):
            if cancel_event.is_set():
                warnings.append("cancelled")
                break
            try:
                data, content_type = self.scraper.fetch_bytes_with_type(image_url)
            except ScraperError as exc:
                warnings.append(f"page {page_index} failed: {exc}")
                callbacks.log(f"  page {page_index}/{len(image_urls)} FAILED: {exc}")
                continue
            if not looks_like_image(data):
                warnings.append(f"page {page_index} was not a valid image")
                callbacks.log(f"  page {page_index}/{len(image_urls)} skipped (not an image)")
                continue
            pages.append((data, guess_extension(image_url, content_type)))
            callbacks.on_page_progress and callbacks.on_page_progress(page_index, len(image_urls))

        if cancel_event.is_set():
            callbacks.log(f"Cancelled while downloading {chapter.label}")
            result = ChapterResult(chapter=chapter, ok=False, error="Cancelled", warnings=warnings)
            callbacks.on_chapter_done and callbacks.on_chapter_done(index, total, result)
            return result

        if not pages:
            result = ChapterResult(chapter=chapter, ok=False,
                                   error="No pages could be downloaded", warnings=warnings)
            callbacks.on_chapter_done and callbacks.on_chapter_done(index, total, result)
            return result

        # 4. pack into a .cbz (write to temp file, then atomic rename)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=final_path.stem + ".", suffix=".cbz.part",
                                            dir=str(final_path.parent))
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            with ZipFile(tmp_path, "w") as archive:
                if cover is not None:
                    archive.writestr(f"000_cover{cover[1]}", cover[0], compress_type=ZIP_STORED)
                pad = max(3, len(str(len(pages))))
                for page_index, (data, ext) in enumerate(pages, start=1):
                    archive.writestr(f"{page_index:0{pad}d}{ext}", data, compress_type=ZIP_STORED)
                archive.writestr("ComicInfo.xml", build_comic_info(manga, chapter, len(pages)),
                                 compress_type=ZIP_DEFLATED)
            os.replace(tmp_path, final_path)
        except Exception as exc:  # noqa: BLE001 - report any failure but keep the batch going
            tmp_path.unlink(missing_ok=True)
            result = ChapterResult(chapter=chapter, ok=False, error=f"Could not write .cbz: {exc}",
                                   warnings=warnings)
            callbacks.on_chapter_done and callbacks.on_chapter_done(index, total, result)
            return result

        size_mb = final_path.stat().st_size / (1024 * 1024)
        callbacks.log(f"Saved {final_path.name}  ({len(pages)} pages, {size_mb:.1f} MB)")
        result = ChapterResult(chapter=chapter, ok=True, path=final_path, pages=len(pages),
                               warnings=warnings)
        callbacks.on_chapter_done and callbacks.on_chapter_done(index, total, result)
        return result

    def download_chapters(self, manga: MangaInfo, chapters: list[ChapterInfo],
                          output_dir: Path, cancel_event: threading.Event,
                          callbacks: DownloadCallbacks) -> BatchSummary:
        summary = BatchSummary(total=len(chapters))
        if not chapters:
            callbacks.log("Nothing to download — no chapters selected.")
            return summary

        callbacks.log(f"Starting download of {len(chapters)} chapter(s) into {output_dir}")
        # Download in reading order (oldest → newest) so libraries index nicely.
        ordered = sorted(chapters, key=lambda ch: (ch.number is None, ch.number or 0.0, ch.label))
        for index, chapter in enumerate(ordered, start=1):
            if cancel_event.is_set():
                summary.cancelled = True
                break
            result = self.download_chapter(manga, chapter, output_dir, cancel_event,
                                           callbacks, index, len(ordered))
            if result.ok:
                if result.skipped:
                    summary.skipped += 1
                else:
                    summary.succeeded += 1
            else:
                if result.error == "Cancelled":
                    summary.cancelled = True
                    break
                summary.failed.append((chapter, result.error))

        if summary.cancelled:
            callbacks.log("Download cancelled by user.")
        else:
            callbacks.log(
                f"Finished: {summary.succeeded} downloaded, {summary.skipped} skipped, "
                f"{len(summary.failed)} failed."
            )
        return summary
