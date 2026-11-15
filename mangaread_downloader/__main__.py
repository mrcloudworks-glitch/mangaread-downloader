"""Entry point: ``python -m mangaread_downloader`` or the ``mangaread-downloader`` script.

Without flags this launches the desktop GUI. ``--cli`` runs a headless download
(handy on SteamOS Desktop Mode, or over SSH).
"""
from __future__ import annotations

import argparse
import sys

from . import __app_name__, __version__
from .config import load_settings, setup_logging


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mangaread-downloader",
        description=f"{__app_name__} v{__version__} — download mangaread.org chapters as .cbz (with cover).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--cli", action="store_true",
                        help="headless mode, no GUI (prints progress to stdout)")
    parser.add_argument("url", nargs="?", help="manga page or chapter page URL")
    parser.add_argument("--output", "-o", help="output directory (default: Downloads folder)")
    parser.add_argument("--chapters", "-c",
                        help="only these chapters, e.g. '1-10' or '1,3,7.5' (manga URLs only)")
    parser.add_argument("--no-merge", action="store_true",
                        help="bulk mode: one .cbz per chapter instead of a single merged .cbz")
    return parser.parse_args(argv)


def run_cli(args: argparse.Namespace) -> int:
    from .cli import run_cli as _run_cli

    return _run_cli(args)


def run_gui() -> int:
    try:
        from .gui import launch_gui
    except ImportError as exc:
        print(
            f"Could not start the GUI: {exc}\n\n"
            "Tkinter is required. Install it with your package manager:\n"
            "  Windows : reinstall Python from python.org and tick 'tcl/tk and IDLE'\n"
            "  Debian/Ubuntu/SteamOS (pacman: sudo pacman -S tk)\n"
            "  Fedora  : sudo dnf install python3-tkinter\n\n"
            "Or use headless mode:  mangaread-downloader --cli <url>",
            file=sys.stderr,
        )
        return 2
    return launch_gui()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    setup_logging()
    if args.cli:
        if not args.url:
            print("error: --cli requires a URL\n", file=sys.stderr)
            print("example: mangaread-downloader --cli https://www.mangaread.org/manga/hundred-years-game/", file=sys.stderr)
            return 2
        return run_cli(args)
    _ = load_settings()  # make sure a corrupt config is reported early
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
