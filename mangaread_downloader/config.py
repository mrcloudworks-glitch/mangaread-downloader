"""Persisted settings (JSON) for the app. Cross-platform, no third-party deps."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .utils import config_dir, default_output_dir

log = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
LOG_FILE = "mangaread-downloader.log"


@dataclass
class Settings:
    output_dir: str = field(default_factory=lambda: str(default_output_dir()))
    # Seconds to wait between HTTP requests (be polite to the site).
    delay: float = 0.25
    # Per-request timeout in seconds.
    timeout: float = 30.0
    # How many times to retry a failed image/page before giving up.
    retries: int = 3
    # Skip chapters whose .cbz already exists instead of re-downloading.
    skip_existing: bool = True
    # Open the output folder when a batch finishes.
    open_folder_when_done: bool = False
    # Last download mode used in the GUI ('single' or 'bulk').
    mode: str = "bulk"

    @classmethod
    def from_dict(cls, data: dict) -> Settings:
        valid = {f.name for f in fields(cls)}
        cleaned = {k: v for k, v in (data or {}).items() if k in valid}
        try:
            return cls(**cleaned)
        except TypeError:
            return cls()


def config_path() -> Path:
    return config_dir() / CONFIG_FILE


def log_path() -> Path:
    return config_dir() / LOG_FILE


def load_settings() -> Settings:
    path = config_path()
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return Settings.from_dict(data)
    except (OSError, ValueError, TypeError) as exc:  # corrupt config should never kill the app
        log.warning("Could not read config %s: %s", path, exc)
    return Settings()


def save_settings(settings: Settings) -> None:
    directory = config_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / (CONFIG_FILE + ".tmp")
        tmp.write_text(json.dumps(asdict(settings), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, directory / CONFIG_FILE)
    except OSError as exc:
        log.warning("Could not save config to %s: %s", directory, exc)


def setup_logging() -> Path:
    """Log to a file in the config dir (GUI also mirrors to its log view)."""
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / LOG_FILE
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == str(log_file)
               for h in root.handlers):
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(handler)
    return log_file
