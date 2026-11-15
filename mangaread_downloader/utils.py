"""Small cross-platform helpers: filename safety, config dir, chapter numbers."""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

# Characters that are illegal in file names on Windows (and annoying elsewhere).
_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows reserved device names (CON, PRN, AUX, NUL, COM1..9, LPT1..9)
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_INVALID_FS_CHARS_SUBS = re.compile(r'\s+')


def sanitize_filename(name: str, fallback: str = "untitled", max_length: int = 180) -> str:
    """Make *name* safe to use as a file or folder name on Windows and Linux."""
    name = (name or "").strip()
    name = _INVALID_FS_CHARS.sub("_", name)
    name = _INVALID_FS_CHARS_SUBS.sub(" ", name).strip(" .")
    if not name:
        name = fallback
    # Windows: trailing dots/spaces are not allowed, and reserved device names are banned.
    if name.upper() in _RESERVED_NAMES:
        name = f"_{name}"
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    return name


def config_dir() -> Path:
    """Per-user config directory (not inside the repo)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "MangaReadDownloader"
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "mangaread-downloader"


def default_output_dir() -> Path:
    """Best guess for the user's downloads folder (works on SteamOS too)."""
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        return downloads
    return Path.home()


_CHAPTER_NUM_PATTERNS = (
    # chapter-23, chapter_23, chapter 23, ch-23, ch23
    re.compile(r"(?:chapter|chap|ch|episode|ep)[-_ ]?(\d+(?:\.\d+)?)", re.IGNORECASE),
    # bare number somewhere in the slug: .../chapter-23/ handled above; this is a fallback
    re.compile(r"(\d+(?:\.\d+)?)"),
)


def parse_chapter_number(slug: str) -> float | None:
    """Extract the numeric chapter number from a slug/URL segment, or None."""
    for pattern in _CHAPTER_NUM_PATTERNS:
        match = pattern.search(slug or "")
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def format_chapter_number(number: float | None, slug: str = "") -> str:
    """Format a chapter number for file names: 23 -> '023', 23.5 -> '023.5'."""
    if number is None:
        return sanitize_filename(slug or "unknown", fallback="unknown")
    if float(number).is_integer():
        return f"{int(number):03d}"
    # Keep the decimal part, zero-pad the integer part.
    whole, _, frac = f"{number:.10g}".partition(".")
    return f"{int(whole):03d}.{frac}"


def natural_chapter_key(number: float | None, title: str):
    """Sort key: numbered chapters first (ascending), unnamed last alphabetically."""
    return (number is None, number if number is not None else 0.0, title.lower())


def strip_thumbnail_size(url: str) -> str:
    """Turn a WordPress '...-193x278.jpg' thumbnail URL into the original '...jpg'."""
    return re.sub(r"-\d{2,4}x\d{2,4}(?=\.[A-Za-z0-9]{2,5}(?:\?.*)?$)", "", url)


def guess_extension(url: str, content_type: str | None = None, default: str = ".jpg") -> str:
    """Pick a file extension for an image from its Content-Type or URL."""
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        mapping = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/avif": ".avif",
            "image/bmp": ".bmp",
        }
        if ct in mapping:
            return mapping[ct]
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return default


def looks_like_image(data: bytes) -> bool:
    """Cheap magic-byte sniff so we don't save HTML error pages as .jpg."""
    if not data or len(data) < 12:
        return False
    if data.startswith(b"\xff\xd8\xff"):  # JPEG
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):  # GIF
        return True
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":  # WebP
        return True
    # ISO-BMFF container (AVIF / HEIC) or BMP
    return data[4:8] == b"ftyp" or data.startswith(b"BM")


def open_in_file_manager(path: Path) -> None:
    """Reveal *path* in the native file manager (Windows / Linux / macOS)."""
    import subprocess

    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:  # Linux / SteamOS (KDE Plasma on SteamOS Desktop Mode)
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:  # noqa: BLE001 - not critical: user can open the folder manually
        logging.getLogger(__name__).info("Could not open file manager for %s: %s", path, exc)
