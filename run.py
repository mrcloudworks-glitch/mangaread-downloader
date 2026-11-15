#!/usr/bin/env python3
"""Convenience launcher: ``python3 run.py`` starts the GUI.

Also usable with ``--cli`` for headless downloads:
    python3 run.py --cli https://www.mangaread.org/manga/hundred-years-game/
"""
from mangaread_downloader.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
