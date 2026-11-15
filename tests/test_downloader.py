"""Tests for the .cbz building pipeline (network mocked out)."""
from __future__ import annotations

import threading
from pathlib import Path
from zipfile import ZipFile

import pytest

from mangaread_downloader.cli import parse_chapter_filter
from mangaread_downloader.config import Settings
from mangaread_downloader.downloader import (
    DownloadCallbacks,
    DownloadManager,
    build_comic_info,
    chapter_cbz_path,
)
from mangaread_downloader.models import ChapterInfo, MangaInfo

FIXTURES = Path(__file__).parent / "fixtures"
CHAPTER_HTML = (FIXTURES / "chapter_page.html").read_text(encoding="utf-8")

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 128
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128


def sample_manga() -> MangaInfo:
    return MangaInfo(
        title="Sample: The/Best? Manga",   # characters hostile to filesystems on purpose
        url="https://www.mangaread.org/manga/sample/",
        slug="sample",
        cover_url="https://www.mangaread.org/uploads/cover.jpg",
        authors=["Author One"],
        artists=["Artist Two"],
        genres=["Action"],
        summary="A test manga.",
        status="Ongoing",
    )


def sample_chapter() -> ChapterInfo:
    return ChapterInfo(title="Chapter 1",
                      url="https://www.mangaread.org/manga/sample/chapter-1/",
                      number=1.0)


class FakeScraper:
    """Stand-in for MangaReadScraper serving fixtures/fake bytes."""

    def __init__(self, html: str = CHAPTER_HTML):
        self.html = html
        self.image_calls: list[str] = []

    def fetch_text(self, url: str) -> str:
        assert "chapter-1" in url
        return self.html

    def fetch_bytes_with_type(self, url: str):
        self.image_calls.append(url)
        if "cover" in url:
            return PNG, "image/png"
        return JPEG, "image/jpeg"


def collect_callbacks():
    messages = {"logs": [], "pages": [], "chapters": []}
    callbacks = DownloadCallbacks(
        on_log=messages["logs"].append,
        on_page_progress=lambda done, total: messages["pages"].append((done, total)),
        on_chapter_done=lambda index, total, result: messages["chapters"].append(result),
    )
    return callbacks, messages


def make_manager(tmp_settings: Settings, fake: FakeScraper) -> DownloadManager:
    manager = DownloadManager(tmp_settings)
    manager.scraper = fake
    return manager


def test_build_comic_info_is_valid_xml():
    import xml.etree.ElementTree as ET

    data = build_comic_info(sample_manga(), sample_chapter(), page_count=45)
    root = ET.fromstring(data)
    assert root.tag == "ComicInfo"
    assert root.findtext("Series") == sample_manga().title
    assert root.findtext("Number") == "1"
    assert root.findtext("Writer") == "Author One"
    assert root.findtext("PageCount") == "45"


def test_chapter_cbz_path_sanitises():
    path = chapter_cbz_path(sample_manga(), sample_chapter(), Path("/out"))
    assert path.suffix == ".cbz"
    assert "Sample_ The_Best_ Manga - Chapter 001.cbz" == path.name
    assert path.parent.name == "Sample_ The_Best_ Manga"


def test_download_chapter_builds_cbz_with_cover_first(tmp_path):
    settings = Settings(output_dir=str(tmp_path), skip_existing=False, delay=0)
    fake = FakeScraper()
    manager = make_manager(settings, fake)
    callbacks, messages = collect_callbacks()

    result = manager.download_chapter(sample_manga(), sample_chapter(), tmp_path,
                                      threading.Event(), callbacks, index=1, total=1)

    assert result.ok, result.error
    assert result.pages == 4  # fixture has 4 usable images (spacer/gravatar/dupes dropped)
    assert result.path.exists()

    with ZipFile(result.path) as archive:
        names = archive.namelist()
        assert names[0].startswith("000_cover")
        assert names[-1] == "ComicInfo.xml"
        assert names[1:-1] == sorted(names[1:-1])  # 001..N in order

    # cover fetched exactly once even though it would be used by every chapter
    assert sum("cover" in url for url in fake.image_calls) == 1
    assert messages["pages"], "page progress callbacks should have fired"


def test_skip_existing_cbz(tmp_path):
    settings = Settings(output_dir=str(tmp_path), skip_existing=True, delay=0)
    fake = FakeScraper()
    manager = make_manager(settings, fake)
    callbacks, _ = collect_callbacks()

    first = manager.download_chapter(sample_manga(), sample_chapter(), tmp_path,
                                     threading.Event(), callbacks, 1, 1)
    assert first.ok and not first.skipped

    calls_before = len(fake.image_calls)
    second = manager.download_chapter(sample_manga(), sample_chapter(), tmp_path,
                                      threading.Event(), callbacks, 1, 1)
    assert second.ok and second.skipped
    assert len(fake.image_calls) == calls_before  # nothing re-downloaded


def test_cancel_event_stops_batch(tmp_path):
    settings = Settings(output_dir=str(tmp_path), skip_existing=False, delay=0)
    fake = FakeScraper()
    manager = make_manager(settings, fake)
    callbacks, _ = collect_callbacks()

    chapters = [ChapterInfo(title="Chapter 1",
                            url="https://www.mangaread.org/manga/sample/chapter-1/",
                            number=1.0)]

    cancel = threading.Event()
    cancel.set()  # cancel before we even start
    summary = manager.download_chapters(sample_manga(), chapters, tmp_path, cancel, callbacks)
    assert summary.cancelled
    assert summary.succeeded == 0


def test_parse_chapter_filter():
    assert parse_chapter_filter("1-3") == [(1.0, 3.0)]
    assert parse_chapter_filter("1, 5, 7-9") == [(1.0, 1.0), (5.0, 5.0), (7.0, 9.0)]
    assert parse_chapter_filter("10-5") == [(5.0, 10.0)]  # reversed bounds are tolerated
    with pytest.raises(ValueError):
        parse_chapter_filter("abc")
