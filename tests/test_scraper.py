"""Tests for URL classification, fixture parsing and filename helpers."""
from __future__ import annotations

from pathlib import Path

import pytest

from mangaread_downloader.scraper import (
    ScraperError,
    classify_url,
    normalize_url,
    parse_chapter_list,
    parse_chapter_page,
    parse_manga_page,
)
from mangaread_downloader.utils import (
    format_chapter_number,
    guess_extension,
    looks_like_image,
    parse_chapter_number,
    sanitize_filename,
    strip_thumbnail_size,
)

FIXTURES = Path(__file__).parent / "fixtures"
MANGA_HTML = (FIXTURES / "manga_page.html").read_text(encoding="utf-8")
CHAPTER_HTML = (FIXTURES / "chapter_page.html").read_text(encoding="utf-8")
MANGA_URL = "https://www.mangaread.org/manga/hundred-years-game/"


# --------------------------------------------------------------- URL logic

@pytest.mark.parametrize("raw,expected", [
    ("https://www.mangaread.org/manga/hundred-years-game/", MANGA_URL),
    ("https://www.mangaread.org/manga/hundred-years-game", MANGA_URL),
    ("http://mangaread.org/manga/hundred-years-game/", MANGA_URL),
    ("mangaread.org/manga/hundred-years-game/", MANGA_URL),
    ("www.mangaread.org/manga/hundred-years-game/?fbclid=abc#x", MANGA_URL),
])
def test_normalize_manga_url(raw, expected):
    assert normalize_url(raw) == expected


def test_normalize_rejects_other_hosts():
    with pytest.raises(ScraperError):
        normalize_url("https://example.com/manga/hundred-years-game/")


def test_classify_manga_and_chapter():
    assert classify_url(MANGA_URL) == ("manga", "hundred-years-game", None)
    kind, slug, segment = classify_url("https://www.mangaread.org/manga/hundred-years-game/chapter-23/")
    assert (kind, slug, segment) == ("chapter", "hundred-years-game", "chapter-23")


def test_classify_rejects_garbage():
    with pytest.raises(ScraperError):
        classify_url("https://www.mangaread.org/genres/action/")
    with pytest.raises(ScraperError):
        classify_url("")


# --------------------------------------------------------- manga HTML page

def test_parse_manga_page_metadata():
    manga = parse_manga_page(MANGA_HTML, MANGA_URL)
    assert manga.title == "Hundred Years Game"
    assert manga.authors == ["Haram"]
    assert manga.artists == ["Jiah"]
    assert manga.genres == ["Action", "Martial Arts"]
    assert manga.status == "Ongoing"
    assert manga.rating == "3.9"
    assert "end of your game" in manga.summary


def test_parse_manga_cover_uses_full_size():
    manga = parse_manga_page(MANGA_HTML, MANGA_URL)
    assert manga.cover_thumb_url.endswith("Read-Manhwa-193x278.png")
    assert manga.cover_url.endswith("/Read-Manhwa.png")


def test_parse_manga_chapter_list():
    manga = parse_manga_page(MANGA_HTML, MANGA_URL)
    numbers = [c.number for c in manga.chapters]
    assert numbers == [1.0, 2.0, 2.5, 3.0, 10.0]  # ascending, deduped, feed/comments excluded
    first = manga.chapters[0]
    assert first.title == "Chapter 1"
    assert first.release_date == "16.08.2020"
    assert all(c.url.startswith("https://") for c in manga.chapters)


def test_parse_chapter_list_from_ajax_fragment():
    fragment = '<li class="wp-manga-chapter"><a href="https://www.mangaread.org/manga/x/chapter-42/">Chapter 42</a></li>'
    chapters = parse_chapter_list(fragment, "x", "https://www.mangaread.org/manga/x/")
    assert [c.number for c in chapters] == [42.0]


# -------------------------------------------------------- chapter HTML page

def test_parse_chapter_page_images_in_order():
    urls, title = parse_chapter_page(CHAPTER_HTML,
                                     "https://www.mangaread.org/manga/hundred-years-game/chapter-1/")
    assert title == "Hundred Years Game - Chapter 1"
    assert urls == [
        "https://www.mangaread.org/wp-content/uploads/WP-manga/data/manga_deadbeef/abc123/2.jpg",
        "https://www.mangaread.org/wp-content/uploads/WP-manga/data/manga_deadbeef/abc123/3.jpg",
        "https://www.mangaread.org/wp-content/uploads/WP-manga/data/manga_deadbeef/abc123/4.jpg",
        "https://cdn.mangaread.org/uploads/WP-manga/data/manga_deadbeef/abc123/5.webp",
    ]


def test_parse_chapter_page_without_reading_content_falls_back():
    html = '<html><body><img src="https://www.mangaread.org/wp-content/uploads/WP-manga/data/x/y/1.jpg"></body></html>'
    urls, _ = parse_chapter_page(html, "https://www.mangaread.org/manga/x/chapter-1/")
    assert urls == ["https://www.mangaread.org/wp-content/uploads/WP-manga/data/x/y/1.jpg"]


def test_parse_chapter_page_no_images_raises():
    with pytest.raises(ScraperError):
        parse_chapter_page("<html><body>nothing here</body></html>",
                           "https://www.mangaread.org/manga/x/chapter-1/")


# ------------------------------------------------------------- utils tests

@pytest.mark.parametrize("raw,expected", [
    ('a<b>c:d"e/f\\g|h?i*j', "a_b_c_d_e_f_g_h_i_j"),
    ("trailing dots...", "trailing dots"),
    ("   spaced   ", "spaced"),
    ("", "untitled"),
])
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_reserved_and_length():
    assert sanitize_filename("CON") == "_CON"
    assert len(sanitize_filename("x" * 500)) <= 180


@pytest.mark.parametrize("slug,expected", [
    ("chapter-23", 23.0),
    ("chapter_2.5", 2.5),
    ("ch-7", 7.0),
    ("Chapter 118/", 118.0),
    ("no-numbers-here", None),
])
def test_parse_chapter_number(slug, expected):
    assert parse_chapter_number(slug) == expected


@pytest.mark.parametrize("number,expected", [
    (1.0, "001"),
    (23.0, "023"),
    (2.5, "002.5"),
    (118.0, "118"),
    (None, "untitled"),
])
def test_format_chapter_number(number, expected):
    assert format_chapter_number(number, "untitled") == expected


def test_strip_thumbnail_size():
    assert strip_thumbnail_size("https://x/Read-Manhwa-193x278.png") == "https://x/Read-Manhwa.png"
    assert strip_thumbnail_size("https://x/plain.jpg") == "https://x/plain.jpg"


def test_guess_extension():
    assert guess_extension("https://x/a.PNG") == ".png"
    assert guess_extension("https://x/a", "image/webp") == ".webp"
    assert guess_extension("https://x/a.jpeg") == ".jpg"


def test_looks_like_image():
    assert looks_like_image(b"\xff\xd8\xff\xe0" + b"0" * 64)
    assert looks_like_image(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    assert not looks_like_image(b"<html><body>404</body></html>")
