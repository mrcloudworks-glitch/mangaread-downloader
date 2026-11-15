"""Headless (--cli) download mode."""
from __future__ import annotations

import re
import sys
import threading
from pathlib import Path

from .config import load_settings
from .downloader import DownloadCallbacks, DownloadManager
from .models import ChapterInfo
from .scraper import (
    MangaReadScraper,
    ScraperError,
    classify_url,
    manga_url_for,
    normalize_url,
)
from .utils import default_output_dir


def parse_chapter_filter(spec: str) -> list[tuple[float, float]]:
    """Parse '1-10,12,15.5' into a list of (low, high) inclusive ranges."""
    ranges: list[tuple[float, float]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", part)
        if match:
            low, high = float(match.group(1)), float(match.group(2))
            ranges.append((min(low, high), max(low, high)))
        elif re.fullmatch(r"\d+(?:\.\d+)?", part):
            value = float(part)
            ranges.append((value, value))
        else:
            raise ValueError(f"Cannot understand chapter spec fragment: {part!r}")
    return ranges


def _in_ranges(chapter: ChapterInfo, ranges: list[tuple[float, float]]) -> bool:
    if chapter.number is None:
        return True  # keep unnumbered chapters rather than silently dropping them
    return any(low <= chapter.number <= high for low, high in ranges)


def run_cli(args) -> int:
    settings = load_settings()
    output = Path(args.output).expanduser() if args.output else Path(settings.output_dir or default_output_dir())

    try:
        kind, slug, _segment = classify_url(args.url)
    except ScraperError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    scraper = MangaReadScraper(delay=settings.delay, timeout=settings.timeout, retries=settings.retries)
    cancel = threading.Event()

    try:
        if kind == "chapter":
            manga = scraper.load_manga(manga_url_for(slug))
            chapter_url = normalize_url(args.url)
            chapter = next((c for c in manga.chapters if c.url.rstrip("/") == chapter_url.rstrip("/")), None)
            if chapter is None:
                # chapter not in the list (unusual): synthesise one from the URL
                from .utils import parse_chapter_number
                chapter = ChapterInfo(title=f"Chapter {parse_chapter_number(chapter_url) or ''}".strip(),
                                      url=chapter_url,
                                      number=parse_chapter_number(chapter_url))
            chapters = [chapter]
        else:
            manga = scraper.load_manga(args.url)
            chapters = list(manga.chapters)
            if args.chapters:
                try:
                    ranges = parse_chapter_filter(args.chapters)
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                chapters = [c for c in chapters if _in_ranges(c, ranges)]
    except ScraperError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not chapters:
        print("No chapters matched.", file=sys.stderr)
        return 1

    print(f"{manga.title}: {len(chapters)} chapter(s) -> {output}")

    callbacks = DownloadCallbacks(
        on_log=lambda msg: print(msg),
        on_page_progress=lambda done, total: print(f"    page {done}/{total}", end="\r"),
    )
    manager = DownloadManager(settings)
    try:
        summary = manager.download_chapters(manga, chapters, output, cancel, callbacks)
    except KeyboardInterrupt:
        cancel.set()
        print("\nInterrupted.")
        return 130

    print()
    print(f"Done: {summary.succeeded} downloaded, {summary.skipped} skipped, "
          f"{len(summary.failed)} failed.")
    if summary.failed:
        for chapter, error in summary.failed:
            print(f"  FAILED {chapter.label}: {error}")
        return 1
    return 0
