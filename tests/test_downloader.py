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
    build_collection_comic_info,
    build_comic_info,
    chapter_cbz_path,
    merged_cbz_path,
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
    """Stand-in for MangaReadScraper serving fixtures/fake bytes.

    fail_chapters: URL fragments for which fetch_text should raise ScraperError.
    """

    def __init__(self, html: str = CHAPTER_HTML, fail_chapters: tuple[str, ...] = ()):
        self.html = html
        self.fail_chapters = fail_chapters
        self.image_calls: list[str] = []

    def fetch_text(self, url: str) -> str:
        for fragment in self.fail_chapters:
            if fragment in url:
                from mangaread_downloader.scraper import ScraperError
                raise ScraperError(f"simulated failure for {url}")
        assert "chapter-" in url
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


# ------------------------------------------------------------- merged bulk

def two_chapters():
    return [
        ChapterInfo(title="Chapter 1",
                    url="https://www.mangaread.org/manga/sample/chapter-1/", number=1.0),
        ChapterInfo(title="Chapter 2",
                    url="https://www.mangaread.org/manga/sample/chapter-2/", number=2.0),
    ]


def manga_with_chapters():
    manga = sample_manga()
    manga.chapters = two_chapters()
    return manga


def test_merged_cbz_path_naming():
    chapters = [ChapterInfo(title="Chapter 2.5",
                            url="https://www.mangaread.org/manga/sample/chapter-2.5/", number=2.5),
                ChapterInfo(title="Chapter 3",
                            url="https://www.mangaread.org/manga/sample/chapter-3/", number=3.0)]
    path = merged_cbz_path(sample_manga(), chapters, Path("/out"))
    assert path.name == "Sample_ The_Best_ Manga - Chapters 002.5-003.cbz"
    assert path.parent.name == "Sample_ The_Best_ Manga"


def test_build_collection_comic_info():
    import xml.etree.ElementTree as ET

    manga = manga_with_chapters()
    chapters = two_chapters()
    data = build_collection_comic_info(manga, chapters, page_count=90)
    root = ET.fromstring(data)
    assert root.tag == "ComicInfo"
    assert root.findtext("Series") == manga.title
    assert "Complete Collection" in (root.findtext("Title") or "")
    assert root.findtext("PageCount") == "90"
    assert root.findtext("Count") == "2"


def test_merged_download_builds_single_cbz(tmp_path):
    settings = Settings(output_dir=str(tmp_path), skip_existing=False, delay=0)
    fake = FakeScraper()
    manager = make_manager(settings, fake)
    callbacks, _messages = collect_callbacks()

    manga = manga_with_chapters()
    summary = manager.download_merged(manga, two_chapters(), tmp_path,
                                      threading.Event(), callbacks)

    assert summary.merged_path is not None and summary.merged_path.exists()
    assert summary.succeeded == 2 and not summary.failed
    assert summary.merged_path.name == "Sample_ The_Best_ Manga - Chapters 001-002.cbz"

    with ZipFile(summary.merged_path) as archive:
        names = archive.namelist()
        assert names[0].startswith("000_cover")
        assert names[-1] == "ComicInfo.xml"
        pages = names[1:-1]
        # fake scraper serves jpeg bytes for every page, so all entries are .jpg
        assert pages == [f"{i:04d}.jpg" for i in range(1, 9)]


def test_merged_skip_existing(tmp_path):
    settings = Settings(output_dir=str(tmp_path), skip_existing=True, delay=0)
    fake = FakeScraper()
    manager = make_manager(settings, fake)
    callbacks, _ = collect_callbacks()

    manga = manga_with_chapters()
    first = manager.download_merged(manga, two_chapters(), tmp_path,
                                    threading.Event(), callbacks)
    assert first.merged_path is not None

    calls_before = len(fake.image_calls)
    second = manager.download_merged(manga, two_chapters(), tmp_path,
                                     threading.Event(), callbacks)
    assert second.succeeded == 0 and second.skipped == 2
    assert second.merged_path == first.merged_path
    assert len(fake.image_calls) == calls_before


def test_merged_partial_chapter_failure_still_builds(tmp_path):
    settings = Settings(output_dir=str(tmp_path), skip_existing=False, delay=0)
    fake = FakeScraper(fail_chapters=("chapter-2",))
    manager = make_manager(settings, fake)
    callbacks, _ = collect_callbacks()

    manga = manga_with_chapters()
    summary = manager.download_merged(manga, two_chapters(), tmp_path,
                                      threading.Event(), callbacks)

    assert summary.merged_path is not None and summary.merged_path.exists()
    assert summary.succeeded == 1
    assert [ch.label for ch, _ in summary.failed] == ["Chapter 2"]

    with ZipFile(summary.merged_path) as archive:
        image_entries = [n for n in archive.namelist() if n != "ComicInfo.xml"]
        assert len(image_entries) == 1 + 4  # cover + 4 pages (chapter 2 excluded)


def test_merged_all_chapters_fail(tmp_path):
    settings = Settings(output_dir=str(tmp_path), skip_existing=False, delay=0)
    fake = FakeScraper(fail_chapters=("chapter-",))
    manager = make_manager(settings, fake)
    callbacks, _ = collect_callbacks()

    manga = manga_with_chapters()
    summary = manager.download_merged(manga, two_chapters(), tmp_path,
                                      threading.Event(), callbacks)

    assert summary.merged_path is None
    assert len(summary.failed) == 2
    assert list(tmp_path.rglob("*.cbz")) == []   # nothing half-written left behind
    assert list(tmp_path.rglob("*.part")) == []


def test_merged_cancel_discards_partial_file(tmp_path):
    settings = Settings(output_dir=str(tmp_path), skip_existing=False, delay=0)
    fake = FakeScraper()
    manager = make_manager(settings, fake)
    callbacks, _ = collect_callbacks()

    manga = manga_with_chapters()
    cancel = threading.Event()
    cancel.set()
    summary = manager.download_merged(manga, two_chapters(), tmp_path, cancel, callbacks)
    assert summary.cancelled
    assert summary.merged_path is None
    assert list(tmp_path.rglob("*.cbz")) == []
    assert list(tmp_path.rglob("*.part")) == []
