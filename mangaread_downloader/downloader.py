"""Download chapters and pack them as .cbz files.

Per-chapter .cbz layout (single mode / split bulk mode):

    000_cover.<ext>     <- cover image taken from the manga's source page
    001.jpg, 002.jpg..  <- chapter pages, in reading order
    ComicInfo.xml       <- ComicRack-compatible metadata

Bulk mode merges every selected chapter into ONE .cbz (continuous numbering,
cover first, one ComicInfo.xml for the collection):

    000_cover.<ext>
    0001.jpg ... 0245.webp   <- all pages, in chapter order
    ComicInfo.xml

Files are written via a ``.part`` temp file that is atomically renamed on
completion, so an interrupted run never leaves a corrupt .cbz behind.
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
    # Set when the batch merged chapters into a single .cbz (bulk mode).
    merged_path: Path | None = None

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


def _new_comic_info_tree(title: str, manga: MangaInfo, web_url: str,
                         page_count: int, count: int | None = None) -> bytes:
    root = ET.Element("ComicInfo")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root.set("xmlns:xsd", "http://www.w3.org/2001/XMLSchema")

    def elem(tag: str, text: str | None) -> None:
        if text is not None and str(text):
            node = ET.SubElement(root, tag)
            node.text = str(text)

    elem("Title", title)
    elem("Series", manga.title)
    elem("Summary", (manga.summary or "")[:1000])
    elem("Writer", ", ".join(manga.authors))
    elem("Penciller", ", ".join(manga.artists))
    elem("Genre", ", ".join(manga.genres))
    elem("Web", web_url)
    if count:
        elem("Count", str(count))
    elem("PageCount", str(page_count))
    elem("Manga", "Unknown")
    elem("LanguageISO", "en")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_comic_info(manga: MangaInfo, chapter: ChapterInfo, page_count: int) -> bytes:
    """ComicRack-compatible metadata, nicely escaped by ElementTree."""
    root = ET.Element("ComicInfo")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root.set("xmlns:xsd", "http://www.w3.org/2001/XMLSchema")

    def elem(tag: str, text: str | None) -> None:
        if text is not None and str(text):
            node = ET.SubElement(root, tag)
            node.text = str(text)

    number = f"{chapter.number:g}" if chapter.number is not None else ""
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


def build_collection_comic_info(manga: MangaInfo, chapters: list[ChapterInfo],
                                page_count: int) -> bytes:
    """ComicInfo.xml for a merged (multi-chapter) .cbz collection."""
    numbered = [c.number for c in chapters if c.number is not None]
    covers_all = bool(numbered) and len(chapters) >= len(manga.chapters)
    if covers_all:
        title = f"{manga.title} - Complete Collection"
    elif numbered:
        title = f"{manga.title} - Chapters {min(numbered):g}-{max(numbered):g}"
    else:
        title = f"{manga.title} - Bulk Download"
    return _new_comic_info_tree(title, manga, manga.url, page_count,
                                count=len(manga.chapters) or len(chapters))


def chapter_cbz_path(manga: MangaInfo, chapter: ChapterInfo, output_dir: Path) -> Path:
    series = sanitize_filename(manga.title, fallback="manga")
    number = format_chapter_number(chapter.number, chapter.url.rstrip("/").rsplit("/", 1)[-1])
    filename = sanitize_filename(f"{series} - Chapter {number}.cbz", fallback=f"chapter-{number}.cbz")
    return output_dir / series / filename


def merged_cbz_path(manga: MangaInfo, chapters: list[ChapterInfo], output_dir: Path) -> Path:
    """Filename for the single merged .cbz: '<Series> - Chapters 001-023.cbz'."""
    series = sanitize_filename(manga.title, fallback="manga")
    numbered = [c.number for c in chapters if c.number is not None]
    if numbered:
        first = format_chapter_number(min(numbered))
        last = format_chapter_number(max(numbered))
        filename = f"{series} - Chapters {first}-{last}.cbz"
    else:
        filename = f"{series} - Bulk.cbz"
    return output_dir / series / sanitize_filename(filename)


def _sort_chapters(chapters: list[ChapterInfo]) -> list[ChapterInfo]:
    """Reading order (oldest → newest) so libraries index nicely."""
    return sorted(chapters, key=lambda ch: (ch.number is None, ch.number or 0.0, ch.label))


class DownloadManager:
    def __init__(self, settings: Settings, scraper: MangaReadScraper | None = None):
        self.settings = settings
        self.scraper = scraper or MangaReadScraper(delay=settings.delay, timeout=settings.timeout,
                                                    retries=settings.retries)
        self._cover_cache: tuple[bytes, str] | None | bool = None  # bytes+ext, False = unavailable, None = not tried

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
                    callbacks.log(f"Cover image fetched ({len(data) / 1024:.0f} KB) — it will be the first page of the .cbz")
                    return self._cover_cache
                callbacks.log(f"Cover candidate did not look like an image: {url}")
            except ScraperError as exc:
                callbacks.log(f"Could not download cover from {url}: {exc}")
        callbacks.log("WARNING: no cover image could be downloaded — .cbz files will be built without one")
        self._cover_cache = False
        return None

    # ------------------------------------------------------- page helpers
    def _chapter_image_urls(self, chapter: ChapterInfo) -> list[str]:
        html = self.scraper.fetch_text(chapter.url)
        image_urls, _title = parse_chapter_page(html, chapter.url)
        return image_urls

    def _fetch_page(self, image_url: str, page_index: int, total_pages: int,
                    callbacks: DownloadCallbacks) -> tuple[tuple[bytes, str] | None, str | None]:
        """Download and validate one page. Returns ((data, ext), None) or (None, warning)."""
        try:
            data, content_type = self.scraper.fetch_bytes_with_type(image_url)
        except ScraperError as exc:
            callbacks.log(f"  page {page_index}/{total_pages} FAILED: {exc}")
            return None, f"page {page_index} failed: {exc}"
        if not looks_like_image(data):
            callbacks.log(f"  page {page_index}/{total_pages} skipped (not an image)")
            return None, f"page {page_index} was not a valid image"
        return (data, guess_extension(image_url, content_type)), None

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

        try:
            image_urls = self._chapter_image_urls(chapter)
        except ScraperError as exc:
            callbacks.log(f"FAILED {chapter.label}: {exc}")
            result = ChapterResult(chapter=chapter, ok=False, error=str(exc))
            callbacks.on_chapter_done and callbacks.on_chapter_done(index, total, result)
            return result

        callbacks.log(f"{chapter.label}: {len(image_urls)} pages found")
        cover = self._get_cover(manga, callbacks)

        pages: list[tuple[bytes, str]] = []
        warnings: list[str] = []
        for page_index, image_url in enumerate(image_urls, start=1):
            if cancel_event.is_set():
                warnings.append("cancelled")
                break
            page, warning = self._fetch_page(image_url, page_index, len(image_urls), callbacks)
            if warning:
                warnings.append(warning)
                continue
            if page is not None:
                pages.append(page)
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

        # pack into a .cbz (temp file, then atomic rename)
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
        ordered = _sort_chapters(chapters)
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

    # ------------------------------------------------------ merged (bulk)
    def download_merged(self, manga: MangaInfo, chapters: list[ChapterInfo],
                        output_dir: Path, cancel_event: threading.Event,
                        callbacks: DownloadCallbacks) -> BatchSummary:
        """Download all selected chapters and pack them into ONE .cbz.

        Pages are streamed straight into the archive (never fully buffered), so
        huge merged volumes don't exhaust memory. Failed chapters are skipped
        with a warning; the merged file is still written from the rest.
        """
        summary = BatchSummary(total=len(chapters))
        if not chapters:
            callbacks.log("Nothing to download — no chapters selected.")
            return summary

        final_path = merged_cbz_path(manga, chapters, output_dir)

        if self.settings.skip_existing and final_path.is_file() and final_path.stat().st_size > 0:
            callbacks.log(f"Skipping (already exists): {final_path.name}")
            summary.skipped = len(chapters)
            summary.merged_path = final_path
            return summary

        callbacks.log(f"Merging {len(chapters)} chapter(s) into one .cbz")
        ordered = _sort_chapters(chapters)
        cover = self._get_cover(manga, callbacks)

        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=final_path.stem + ".", suffix=".cbz.part",
                                            dir=str(final_path.parent))
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)

        page_no = 0                     # content pages written (excl. cover)
        included: list[ChapterInfo] = []
        failures: list[tuple[ChapterInfo, str]] = []
        try:
            with ZipFile(tmp_path, "w") as archive:
                if cover is not None:
                    archive.writestr(f"000_cover{cover[1]}", cover[0], compress_type=ZIP_STORED)
                for index, chapter in enumerate(ordered, start=1):
                    if cancel_event.is_set():
                        summary.cancelled = True
                        break
                    callbacks.on_chapter_start and callbacks.on_chapter_start(index, len(ordered), chapter)
                    try:
                        image_urls = self._chapter_image_urls(chapter)
                    except ScraperError as exc:
                        failures.append((chapter, str(exc)))
                        callbacks.log(f"FAILED {chapter.label}: {exc}")
                        result = ChapterResult(chapter=chapter, ok=False, error=str(exc))
                        callbacks.on_chapter_done and callbacks.on_chapter_done(index, len(ordered), result)
                        continue
                    callbacks.log(f"{chapter.label}: {len(image_urls)} pages found")

                    chapter_pages = 0
                    chapter_warnings: list[str] = []
                    for page_index, image_url in enumerate(image_urls, start=1):
                        if cancel_event.is_set():
                            summary.cancelled = True
                            break
                        page, warning = self._fetch_page(image_url, page_index, len(image_urls), callbacks)
                        if warning:
                            chapter_warnings.append(warning)
                            continue
                        if page is None:
                            continue
                        data, ext = page
                        page_no += 1
                        archive.writestr(f"{page_no:04d}{ext}", data, compress_type=ZIP_STORED)
                        chapter_pages += 1
                        callbacks.on_page_progress and callbacks.on_page_progress(page_index, len(image_urls))

                    if summary.cancelled:
                        break
                    if chapter_pages == 0:
                        reason = "No pages could be downloaded"
                        failures.append((chapter, reason))
                        callbacks.log(f"FAILED {chapter.label}: {reason}")
                        result = ChapterResult(chapter=chapter, ok=False,
                                               error=reason, warnings=chapter_warnings)
                        callbacks.on_chapter_done and callbacks.on_chapter_done(index, len(ordered), result)
                        continue
                    included.append(chapter)
                    result = ChapterResult(chapter=chapter, ok=True, pages=chapter_pages,
                                           warnings=chapter_warnings)
                    callbacks.on_chapter_done and callbacks.on_chapter_done(index, len(ordered), result)

                if not summary.cancelled and page_no > 0:
                    archive.writestr(
                        "ComicInfo.xml",
                        build_collection_comic_info(manga, included, page_no),
                        compress_type=ZIP_DEFLATED,
                    )
        except Exception as exc:  # noqa: BLE001 - never leave a half-written archive behind
            tmp_path.unlink(missing_ok=True)
            callbacks.log(f"ERROR: could not write merged .cbz: {exc}")
            summary.failed.extend(failures)
            summary.failed.append((ChapterInfo(title=final_path.name, url=final_path.name, number=None),
                                   f"merged file error: {exc}"))
            return summary

        if summary.cancelled or page_no == 0:
            tmp_path.unlink(missing_ok=True)
            summary.failed.extend(failures)
            if summary.cancelled:
                callbacks.log("Download cancelled — partial merged file discarded.")
            else:
                callbacks.log("ERROR: no pages could be downloaded from any chapter — nothing was saved.")
            return summary

        try:
            os.replace(tmp_path, final_path)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            callbacks.log(f"ERROR: could not move merged file into place: {exc}")
            summary.failed.extend(failures)
            summary.failed.append((ChapterInfo(title=final_path.name, url=final_path.name, number=None),
                                   f"could not save merged file: {exc}"))
            return summary

        summary.succeeded = len(included)
        summary.failed = failures
        summary.merged_path = final_path
        size_mb = final_path.stat().st_size / (1024 * 1024)
        callbacks.log(
            f"Saved merged {final_path.name}  ({len(included)}/{len(ordered)} chapters, "
            f"{page_no} pages, {size_mb:.1f} MB)"
        )
        if failures:
            callbacks.log(f"WARNING: {len(failures)} chapter(s) could not be fetched and were left out of the merged file.")
        return summary
