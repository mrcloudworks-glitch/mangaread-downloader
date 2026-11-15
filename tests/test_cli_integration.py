"""Local end-to-end smoke test of the CLI download pipeline.

Every HTTP request is intercepted by a requests adapter that serves the
saved fixture pages — no network access happens. This exercises the same
code path the GUI uses: URL normalisation, page parsing, image downloads,
CBZ packing, ComicInfo.xml metadata, skip-existing behaviour and the
``--chapters`` / ``--no-merge`` options.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlparse
from zipfile import ZipFile

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.models import Response
from requests.structures import CaseInsensitiveDict

from mangaread_downloader import cli
from mangaread_downloader import downloader as downloader_module
from mangaread_downloader.cli import run_cli
from mangaread_downloader.config import Settings
from mangaread_downloader.scraper import MangaReadScraper

FIXTURES = Path(__file__).parent / "fixtures"
MANGA_HTML = (FIXTURES / "manga_page.html").read_bytes()
CHAPTER_HTML = (FIXTURES / "chapter_page.html").read_bytes()
MANGA_URL = "https://www.mangaread.org/manga/hundred-years-game/"

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x11" * 256
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x22" * 256


def make_response(url: str, body: bytes, content_type: str, status: int = 200) -> Response:
    response = Response()
    response.status_code = status
    response._content = body
    response.headers = CaseInsensitiveDict({"Content-Type": content_type})
    response.url = url
    response.request = requests.Request("GET", url).prepare()
    return response


class FixtureAdapter(BaseAdapter):
    """Serve the fixture site for www.mangaread.org URLs."""

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        path = urlparse(request.url).path
        if path.rstrip("/") == "/manga/hundred-years-game":
            return make_response(request.url, MANGA_HTML, "text/html; charset=utf-8")
        if path.startswith("/manga/hundred-years-game/chapter-") and path.rstrip("/") != "/manga/hundred-years-game":
            return make_response(request.url, CHAPTER_HTML, "text/html; charset=utf-8")
        if "Read-Manhwa.png" in path or "cover" in path.lower():
            return make_response(request.url, PNG_BYTES, "image/png")
        if path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            content_type = "image/webp" if path.lower().endswith(".webp") else "image/jpeg"
            return make_response(request.url, JPEG_BYTES, content_type)
        return make_response(request.url, b"not found", "text/plain", status=404)

    def close(self):  # pragma: no cover - trivial
        pass


def fixture_scraper(**kwargs) -> MangaReadScraper:
    session = requests.Session()
    session.mount("https://", FixtureAdapter())
    session.mount("http://", FixtureAdapter())
    kwargs.setdefault("delay", 0)
    kwargs.setdefault("retries", 0)
    return MangaReadScraper(session=session, **kwargs)


@pytest.fixture()
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "MangaReadScraper", lambda *a, **k: fixture_scraper())
    monkeypatch.setattr(downloader_module, "MangaReadScraper", lambda *a, **k: fixture_scraper())
    monkeypatch.setattr(cli, "load_settings",
                        lambda: Settings(output_dir=str(tmp_path), delay=0.0, retries=0))
    return tmp_path


def args_for(url: str, chapters: str | None = None, output: str | None = None,
             no_merge: bool = False) -> argparse.Namespace:
    return argparse.Namespace(url=url, output=output, chapters=chapters,
                              cli=True, no_merge=no_merge)


def cbz_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.cbz"))


def test_cli_bulk_download_merges_into_single_cbz(offline, capsys):
    rc = run_cli(args_for(MANGA_URL, output=str(offline)))
    assert rc == 0

    produced = cbz_files(offline)
    assert len(produced) == 1
    merged = produced[0]
    assert merged.name == "Hundred Years Game - Chapters 001-010.cbz"
    assert merged.parent.name == "Hundred Years Game"

    with ZipFile(merged) as archive:
        names = archive.namelist()
        assert names[0].startswith("000_cover")      # cover is the first page
        assert names[-1] == "ComicInfo.xml"          # collection metadata
        pages = names[1:-1]
        assert len(pages) == 20                      # 5 chapters × 4 fixture pages
        assert pages == sorted(pages)                # continuous zero-padded order
        assert pages[0] == "0001.jpg"
        assert pages[-1] == "0020.webp"
        info = archive.read("ComicInfo.xml").decode("utf-8")
        assert "<PageCount>20</PageCount>" in info
        assert "Complete Collection" in info   # all 5 fixture chapters selected

    out = capsys.readouterr().out
    assert "Merged 5/5 chapter(s)" in out


def test_cli_chapter_range_filter_merges_subset(offline):
    rc = run_cli(args_for(MANGA_URL, chapters="2-3", output=str(offline)))
    assert rc == 0
    produced = cbz_files(offline)
    assert len(produced) == 1
    assert produced[0].name == "Hundred Years Game - Chapters 002-003.cbz"
    with ZipFile(produced[0]) as archive:
        assert len(archive.namelist()) == 1 + 12 + 1  # cover + 3 chapters × 4 pages + ComicInfo


def test_cli_no_merge_flag_keeps_one_file_per_chapter(offline):
    rc = run_cli(args_for(MANGA_URL, output=str(offline), no_merge=True))
    assert rc == 0
    names = [p.name for p in cbz_files(offline)]
    assert sorted(names) == [
        "Hundred Years Game - Chapter 001.cbz",
        "Hundred Years Game - Chapter 002.5.cbz",
        "Hundred Years Game - Chapter 002.cbz",
        "Hundred Years Game - Chapter 003.cbz",
        "Hundred Years Game - Chapter 010.cbz",
    ]


def test_cli_single_chapter_url(offline):
    chapter_url = "https://www.mangaread.org/manga/hundred-years-game/chapter-1/"
    rc = run_cli(args_for(chapter_url, output=str(offline)))
    assert rc == 0
    names = [p.name for p in cbz_files(offline)]
    assert names == ["Hundred Years Game - Chapter 001.cbz"]


def test_cli_rejects_unknown_host(offline):
    rc = run_cli(args_for("https://evil.example.com/manga/x/", output=str(offline)))
    assert rc != 0
