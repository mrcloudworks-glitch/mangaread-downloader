# MangaRead Downloader

<img src="assets/icon.png" width="96" align="right" alt="MangaRead Downloader icon">

A cross-platform **desktop app (Python + Tkinter)** that downloads manga / manhwa / manhua chapters from
[mangaread.org](https://www.mangaread.org/) and saves them as **`.cbz` files with the series cover image embedded**.

Works on **Windows**, **Linux**, and **SteamOS** (Desktop Mode, e.g. on a Steam Deck).

---

## Features

- **Single Chapter mode** — paste a chapter URL, get exactly one `.cbz`.
- **Bulk Download mode** — paste a manga URL, download all (or a range / a hand-picked subset of) chapters, each as its own `.cbz`.
- **.cbz output** — every file starts with the series cover (`000_cover.*`, taken from the source page) followed by the pages in reading order, plus embedded **ComicInfo.xml** metadata (series, number, writer, genre…) that manga readers such as ComicRack, Komga, Panel Flow or Mihon/Tachiyomi understand.
- **Smart UI** — load the chapter list, tick/untick chapters, pick a numeric range, cover thumbnail preview, overall + per-page progress bars, live log, cancellable downloads.
- **Resume-friendly** — chapters whose `.cbz` already exists are skipped (toggleable).
- **Polite & resilient** — small delay between requests, automatic retries with backoff, atomic `.cbz.part` → `.cbz` rename so an interrupted run never leaves a corrupt file.
- **Headless CLI** for servers / SteamOS terminal use.
- No account, no ads, no telemetry — just Python + two small libraries.

---

## Quick start

### Windows

1. Install [Python 3.9+ from python.org](https://www.python.org/downloads/) and keep **“tcl/tk and IDLE”** ticked in the installer (it ships Tkinter).
2. Download/clone this repository.
3. In a terminal in the repo folder:
   ```bat
   pip install -r requirements.txt
   python run.py
   ```
4. (Optional) Build a stand-alone `MangaReadDownloader.exe`:
   ```bat
   packaging\build_windows.bat
   ```
   → `dist\MangaReadDownloader.exe` (no Python needed on the target PC).

### Linux

Tkinter comes from your distro (not from pip), so install it once:

| Distro | Command |
|---|---|
| Debian / Ubuntu / Mint | `sudo apt install python3-tk python3-venv` |
| Fedora | `sudo dnf install python3-tkinter` |
| Arch / Manjaro | `sudo pacman -S tk` |

Then:

```bash
git clone <repo-url> && cd mangaread-downloader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 run.py
```

To get a `mangaread-downloader` command on PATH: `pip install .`

### SteamOS (Steam Deck — Desktop Mode)

SteamOS is Arch-based with a read-only root. Recommended path (no system changes):

```bash
# Desktop Mode → Konsole
sudo steamos-readonly disable          # needed only for the tk package
sudo pacman -S --needed tk
sudo steamos-readonly enable           # optional: re-lock the root fs

git clone <repo-url> ~/mangaread-downloader && cd ~/mangaread-downloader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 run.py
```

Prefer a **single self-contained binary** instead of touching the system? Build it in Desktop Mode:

```bash
pip install --user pyinstaller
packaging/build_linux.sh               # produces dist/mangaread-downloader with Tk bundled
```

**Tip — play from Game Mode:** right-click `dist/mangaread-downloader` (or `run.py` via a launcher script) in the file manager → *Add to Steam*. It now appears in your library as a non-Steam game and launches full-screen from Game Mode.

Install a desktop entry/icon: `packaging/install_desktop.sh`

---

## Using the app

1. **Choose the mode** at the top: **Single Chapter** or **Bulk Download**.
2. **Paste a URL** and press **Load**:
   - manga page → `https://www.mangaread.org/manga/hundred-years-game/`
   - or chapter page → `https://www.mangaread.org/manga/hundred-years-game/chapter-1/`
   - (a chapter URL automatically switches the app to Single Chapter mode)
3. **Select chapters** (bulk mode): tick rows, *Select all / none / invert*, or type a numeric range (e.g. `1` to `50`) → *Select range*.
4. **Pick the output folder** and press **Start (Bulk) Download**.

Output layout (a subfolder per series):

```
<output>/Hundred Years Game/Hundred Years Game - Chapter 001.cbz
                            Hundred Years Game - Chapter 002.cbz
                            ...
```

Each `.cbz` is a standard ZIP comic archive:

```
000_cover.png   ← series cover, from the source page
001.jpg         ← page 1
002.jpg         ← page 2
…
ComicInfo.xml   ← Series, Number, Writer, Genre, Web, PageCount…
```

Settings (delay between requests, timeout, retries, skip-existing, open folder when done) persist to a per-user config file: `%APPDATA%\MangaReadDownloader\config.json` on Windows, `~/.config/mangaread-downloader/config.json` on Linux/SteamOS. Logs go to the same folder (`mangaread-downloader.log`).

### CLI (no GUI)

```bash
mangaread-downloader --cli https://www.mangaread.org/manga/hundred-years-game/          # all chapters
mangaread-downloader --cli https://www.mangaread.org/manga/hundred-years-game/ -c 1-10  # a range
mangaread-downloader --cli https://www.mangaread.org/manga/hundred-years-game/chapter-3/  # single chapter
mangaread-downloader --cli <url> -o /path/to/folder                                      # custom output dir
```

---

## Building stand-alone executables

| Platform | Script | Output |
|---|---|---|
| Windows | `packaging\build_windows.bat` | `dist\MangaReadDownloader.exe` |
| Linux / SteamOS | `packaging/build_linux.sh` | `dist/mangaread-downloader` |

Icon files (`assets/icon.png`, `assets/icon.ico`) and the desktop entry (`packaging/mangaread-downloader.desktop`) are included.

---

## Troubleshooting

- **“Could not start the GUI: No module named 'tkinter'”** — install the OS package from the tables above (it can’t come from pip), or just use `--cli`.
- **“Could not reach …” / timeouts** — check your connection/VPN; the site may also be rate-limiting — raise *Delay between requests* in Settings.
- **HTTP 403/429** — the site is challenging scripted traffic; increase the delay in Settings and try again later.
- **No chapters found after Load** — the site layout may have changed. Open an issue with the manga URL; the parser selectors live in `mangaread_downloader/scraper.py`.
- **A chapter downloads with no/failed pages** — see the log; failed pages are reported per page and the chapter is marked failed without producing a broken `.cbz`.
- **SteamOS, Game Mode** — the app opens in Desktop Mode semantics; add it as a non-Steam game (see above) for a console-like experience.

---

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                  # 43 tests, incl. an offline end-to-end CLI run against a fixture site
ruff check .
```

```
mangaread_downloader/
├── __main__.py      # entry point (GUI by default, --cli headless mode)
├── cli.py           # headless downloads
├── gui.py           # Tkinter desktop app
├── scraper.py       # mangaread.org HTTP + HTML parsing (pure parse functions, unit-tested)
├── downloader.py    # image downloads, CBZ packing, ComicInfo.xml, batch orchestration
├── config.py        # per-user settings (JSON)
├── models.py        # MangaInfo / ChapterInfo dataclasses
└── utils.py         # filename sanitising, chapter numbers, paths
tests/               # fixtures + offline unit & integration tests
packaging/           # PyInstaller build scripts, .desktop entry
assets/              # app icon (png/ico)
```

## Notes & legal

- This tool is for **personal use**. Downloading chapters may be restricted by the site’s terms of service; content remains the property of the original authors/publishers. Support the creators.
- mangaread.org changes its markup from time to time; if something breaks, please open an issue with the URL and the log file.

## License

MIT — see the repository for details. (No warranty; use at your own risk.)
