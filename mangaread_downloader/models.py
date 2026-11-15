"""Data models shared by the scraper, downloader and GUI."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChapterInfo:
    """A single chapter entry parsed from a manga page."""

    title: str                 # e.g. "Chapter 23"
    url: str                   # absolute URL of the chapter page
    number: float | None       # numeric chapter number if it could be parsed
    release_date: str = ""     # raw date text from the list, if present

    @property
    def label(self) -> str:
        return self.title or self.url.rstrip("/").rsplit("/", 1)[-1]


@dataclass
class MangaInfo:
    """Everything we know about a manga series, parsed from its page."""

    title: str
    url: str
    slug: str
    cover_url: str | None = None        # full-size cover (thumbnail suffix stripped)
    cover_thumb_url: str | None = None  # original <img> src as found on the page
    summary: str = ""
    authors: list[str] = field(default_factory=list)
    artists: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    status: str = "Unknown"
    rating: str = ""
    chapters: list[ChapterInfo] = field(default_factory=list)

    @property
    def chapter_count(self) -> int:
        return len(self.chapters)
