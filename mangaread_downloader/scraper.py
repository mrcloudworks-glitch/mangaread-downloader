"""HTTP client + HTML parsing for mangaread.org.

The parsing functions are deliberately pure (HTML in, data out) so they can be
unit-tested against saved fixtures without network access.
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .models import ChapterInfo, MangaInfo
from .utils import natural_chapter_key, parse_chapter_number, strip_thumbnail_size

log = logging.getLogger(__name__)

BASE_URL = "https://www.mangaread.org"
SITE_NAME = "mangaread.org"

# A browser-ish UA: some CDN front-ends refuse obviously-scripted agents.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_ALLOWED_NETLOCS = {"mangaread.org", "www.mangaread.org"}

_MANGA_PATH_RE = re.compile(r"^/manga/([^/]+)/?$", re.IGNORECASE)
_CHAPTER_PATH_RE = re.compile(r"^/manga/([^/]+)/([^/]+)/?$", re.IGNORECASE)
# Chapter-ish second segment: chapter-23, chapter_23.5, ch-23, episode-7 ...
_CHAPTER_SEGMENT_RE = re.compile(r"^(?:chapter|chap|ch|episode|ep|issue)[-_ ]?[\d][\w.\-]*$", re.IGNORECASE)
# Generic numeric-looking fallback, e.g. "/234/" is unusual but possible.
_NUMERIC_SEGMENT_RE = re.compile(r"^\d+(?:[.\-]\d+)*$")


class ScraperError(Exception):
    """Raised for any user-facing scraping problem (bad URL, site down, ...)."""


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ScraperError("Please enter a mangaread.org URL first.")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url  # tolerate "mangaread.org/manga/..." pastes
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc not in _ALLOWED_NETLOCS:
        raise ScraperError(
            f"This app only works with {SITE_NAME} — got host '{netloc or url}'.\n"
            "Paste a manga page (https://www.mangaread.org/manga/<title>/) "
            "or a chapter page URL."
        )
    # Force https, drop query/fragment noise, canonicalise with a trailing slash.
    path = re.sub(r"/{2,}", "/", parsed.path)
    if not path.endswith("/"):
        path += "/"
    return f"https://www.{SITE_NAME}{path}"


def classify_url(url: str) -> tuple[str, str, str | None]:
    """Return (kind, slug, chapter_segment). kind is 'manga' or 'chapter'."""
    norm = normalize_url(url)
    parsed = urlparse(norm)
    match = _MANGA_PATH_RE.match(parsed.path)
    if match:
        return "manga", match.group(1), None
    match = _CHAPTER_PATH_RE.match(parsed.path)
    if match:
        return "chapter", match.group(1), match.group(2)
    raise ScraperError(
        "Unrecognised mangaread.org URL.\n"
        "Expected a manga page:  https://www.mangaread.org/manga/<title>/\n"
        "or a chapter page:     https://www.mangaread.org/manga/<title>/chapter-1/"
    )


def manga_url_for(slug: str) -> str:
    return f"{BASE_URL}/manga/{slug}/"


class MangaReadScraper:
    def __init__(self, delay: float = 0.25, timeout: float = 30.0, retries: int = 3,
                 session: requests.Session | None = None):
        self.delay = max(0.0, float(delay))
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.session = session if session is not None else requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE_URL + "/",
        })

    # ------------------------------------------------------------------ HTTP
    def _request(self, url: str) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                if attempt:
                    time.sleep(min(2.0 * attempt, 8.0))  # 2s, 4s, 6s... backoff
                response = self.session.get(url, timeout=self.timeout)
                if response.status_code == 404:
                    raise ScraperError(f"Not found (404): {url}")
                if response.status_code in (429, 500, 502, 503, 504):
                    raise ScraperError(f"Server returned HTTP {response.status_code} for {url}")
                response.raise_for_status()
                self._polite_pause()
                return response
            except ScraperError:
                raise
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("Attempt %d/%d failed for %s: %s", attempt + 1, self.retries + 1, url, exc)
                continue
        raise ScraperError(f"Could not reach {url}\n{last_exc}")

    def _polite_pause(self) -> None:
        if self.delay:
            time.sleep(self.delay)

    def fetch_bytes_with_type(self, url: str) -> tuple[bytes, str | None]:
        """Download a binary resource (image/cover). Returns (content, content_type)."""
        response = self._request(url)
        return response.content, response.headers.get("Content-Type")

    def fetch_text(self, url: str) -> str:
        return self._request(url).text

    # ------------------------------------------------------------- parsing
    def load_manga(self, url: str) -> MangaInfo:
        _kind, slug, _segment = classify_url(url)
        page_url = manga_url_for(slug)
        html = self.fetch_text(page_url)
        manga = parse_manga_page(html, page_url)
        if not manga.chapters:
            manga.chapters = self._try_ajax_chapter_list(html, manga)
        return manga

    def load_chapter_page(self, url: str):
        """Fetch a chapter page and return (image_urls, chapter_title)."""
        kind, _slug, _segment = classify_url(url)
        if kind != "chapter":
            raise ScraperError("Expected a chapter URL, e.g. https://www.mangaread.org/manga/<title>/chapter-1/")
        html = self.fetch_text(normalize_url(url))
        return parse_chapter_page(html, normalize_url(url))

    def _try_ajax_chapter_list(self, html: str, manga: MangaInfo) -> list[ChapterInfo]:
        """Best-effort fallback for themes that inject the list via admin-ajax."""
        match = (
            re.search(r'"manga_id"\s*:\s*"?(\d+)"?', html)
            or re.search(r'data-(?:manga-)?id="(\d+)"', html)
            or re.search(r'"post":\s*"?(\d+)"?', html)
        )
        if not match:
            return []
        manga_id = match.group(1)
        try:
            response = self.session.post(
                f"{BASE_URL}/wp-admin/admin-ajax.php",
                data={"action": "manga_get_chapters", "manga_id": manga_id},
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": manga.url},
                timeout=self.timeout,
            )
            if response.ok and "<a" in response.text:
                return parse_chapter_list(response.text, manga.slug, manga.url)
        except requests.RequestException as exc:
            log.info("AJAX chapter-list fallback failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Pure parsing helpers (unit-tested without network)
# ---------------------------------------------------------------------------

def parse_manga_page(html: str, page_url: str) -> MangaInfo:
    soup = BeautifulSoup(html, "html.parser")
    _kind, slug, _segment = classify_url(page_url)

    # --- title
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True).split(" - ")[0].strip()
    title = _clean_title(title) or slug.replace("-", " ").title()

    # --- cover image
    cover_thumb = None
    cover_block = soup.select_one(".summary_image img, .tab-summary img, .profile-manga img")
    if cover_block:
        cover_thumb = _first_attr(cover_block, "data-src", "data-lazy-src", "data-original", "src")
    if not cover_thumb:
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and og.get("content"):
            cover_thumb = og["content"].strip()
    if not cover_thumb:
        candidate = soup.find("img", src=re.compile(r"wp-content/uploads.*\.(jpg|jpeg|png|webp)", re.IGNORECASE))
        if candidate:
            cover_thumb = candidate.get("src", "").strip()
    cover_thumb = urljoin(page_url, cover_thumb) if cover_thumb else None
    cover_full = strip_thumbnail_size(cover_thumb) if cover_thumb else None

    # --- summary / meta fields
    summary = ""
    summary_block = soup.select_one(".summary__content, .description-summary .summary__content, .manga-excerpt")
    if summary_block:
        summary = summary_block.get_text(" ", strip=True)

    def field_text(selector: str) -> str:
        node = soup.select_one(selector)
        return node.get_text(" ", strip=True) if node else ""

    def field_links(selector: str) -> list[str]:
        return [a.get_text(" ", strip=True) for a in soup.select(f"{selector} a") if a.get_text(strip=True)]

    authors = field_links(".author-content") or _split_field(field_text(".author-content"))
    artists = field_links(".artist-content") or _split_field(field_text(".artist-content"))
    genres = field_links(".genres-content") or _split_field(field_text(".genres-content"))

    status = ""
    for node in soup.select(".post-status .summary-content, .post-status, .summary-content"):
        text = node.get_text(" ", strip=True)
        lowered = text.lower()
        if lowered in ("ongoing", "on going", "completed", "hiatus", "cancelled", "dropped", "axed"):
            status = text
            break
    if not status:
        match = re.search(r"\b(OnGoing|Completed|Hiatus|Cancelled|Dropped|Axed)\b", html, re.IGNORECASE)
        status = match.group(1) if match else "Unknown"

    rating = ""
    rating_node = soup.select_one(".rating .score, .post-total-rating .score, .total_votes")
    if rating_node:
        rating = rating_node.get_text(" ", strip=True)
    if not rating:
        match = re.search(r"Average\s+([0-9.]+)\s*/\s*5", html)
        if match:
            rating = match.group(1)

    chapters = parse_chapter_list(html, slug, page_url)

    return MangaInfo(
        title=title,
        url=page_url,
        slug=slug,
        cover_url=cover_full,
        cover_thumb_url=cover_thumb,
        summary=summary,
        authors=authors,
        artists=artists,
        genres=genres,
        status=status.title() if status else "Unknown",
        rating=rating,
        chapters=chapters,
    )


def parse_chapter_list(html_or_soup, slug: str, page_url: str) -> list[ChapterInfo]:
    """Extract unique chapter links for *slug* from manga page (or ajax) HTML."""
    soup = BeautifulSoup(html_or_soup, "html.parser") if isinstance(html_or_soup, str) else html_or_soup
    absolute_slug = f"/manga/{slug}/".lower()
    chapters: dict[str, ChapterInfo] = {}

    for anchor in soup.find_all("a", href=True):
        href = urljoin(page_url, anchor["href"].strip())
        parsed = urlparse(href)
        path = re.sub(r"/{2,}", "/", parsed.path)
        if not path.lower().startswith(absolute_slug):
            continue
        remainder = path[len(absolute_slug):].strip("/")
        if not remainder or "/" in remainder:
            continue
        if remainder.lower() in {"feed", "page", "comments", "rss"}:
            continue
        if not (_CHAPTER_SEGMENT_RE.match(remainder) or _NUMERIC_SEGMENT_RE.match(remainder)):
            continue
        key = f"{parsed.scheme}://{parsed.netloc.lower()}{path if path.endswith('/') else path + '/'}".lower()
        if key in chapters:
            continue
        title = anchor.get_text(" ", strip=True) or remainder.replace("-", " ").title()
        # Release date, if the theme prints one next to the link.
        release_date = ""
        parent_li = anchor.find_parent("li")
        if parent_li:
            date_node = parent_li.select_one(".chapter-release-date, .chapter-release-date a, time")
            if date_node:
                release_date = date_node.get_text(" ", strip=True)
        chapters[key] = ChapterInfo(
            title=_clean_title(title),
            url=key,
            number=parse_chapter_number(remainder),
            release_date=release_date,
        )

    return sorted(chapters.values(), key=lambda ch: natural_chapter_key(ch.number, ch.label))


def parse_chapter_page(html: str, chapter_url: str) -> tuple[list[str], str]:
    """Return (ordered image URLs, page title) for a chapter page."""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    title = _clean_title(title)

    container = soup.select_one(".reading-content, .chapter-content, .entry-content, .post-content")
    image_urls: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        if not raw:
            return
        raw = raw.strip().strip('"').strip("'")
        if not raw or raw.startswith("data:"):
            return
        absolute = urljoin(chapter_url, raw)
        # Drop tracking pixels / icons / 1x1 spacers.
        if re.search(r"(spacer|blank|pixel|loading|emoji|avatar|gravatar|wpdiscuz)", absolute, re.IGNORECASE):
            return
        if absolute not in seen:
            seen.add(absolute)
            image_urls.append(absolute)

    if container is not None:
        for img in container.find_all("img"):
            add(_first_attr(img, "data-src", "data-lazy-src", "data-original", "data-cfsrc", "src"))
        # madara themes sometimes put the URLs in noscript blocks
        if not image_urls:
            for noscript in container.find_all("noscript"):
                for img in BeautifulSoup(noscript.get_text(), "html.parser").find_all("img"):
                    add(_first_attr(img, "src", "data-src"))
    else:
        # No known container: fall back to anything under uploads/WP-manga/data.
        for img in soup.find_all("img"):
            src = _first_attr(img, "data-src", "data-lazy-src", "data-original", "src")
            if src and "wp-manga/data" in src.lower():
                add(src)

    if not image_urls:
        raise ScraperError(
            "No page images found on that chapter page.\n"
            "The site layout may have changed, or the chapter is not available."
        )
    return image_urls, title


# ------------------------------------------------------------------ helpers

def _first_attr(tag, *attrs: str) -> str | None:
    for attr in attrs:
        value = tag.get(attr)
        if value:
            value = str(value).strip()
            if value and not value.startswith("data:image/gif;base64"):
                return value
    return None


def _clean_title(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip(" -–|")
    # Strip common site-name suffixes: "... - MangaRead", "Read ... Online"
    text = re.sub(r"\s*[-–|]\s*Manga ?Read.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^Read\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+Online.*$", "", text, flags=re.IGNORECASE)
    return text.strip(" -–|")


def _split_field(text: str) -> list[str]:
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,/]", text) if part.strip() and part.strip().lower() != "n/a"]
