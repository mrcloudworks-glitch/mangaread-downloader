"""Tkinter desktop GUI for MangaRead Downloader.

Layout: header → mode picker → URL loader → manga info + chapter list
→ output folder → progress → actions → log → status bar.

All network/disk work happens on daemon threads; the UI thread only
consumes messages from ``self.msg_queue`` so the window never freezes.
"""
from __future__ import annotations

import datetime
import logging
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:  # Pillow is optional — enables cover thumbnail preview
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    PIL_AVAILABLE = False

from . import __app_name__, __site__, __version__
from .config import config_path, load_settings, save_settings
from .downloader import DownloadCallbacks, DownloadManager
from .models import ChapterInfo, MangaInfo
from .scraper import (
    MangaReadScraper,
    ScraperError,
    classify_url,
    manga_url_for,
    normalize_url,
)
from .utils import (
    looks_like_image,
    open_in_file_manager,
)

log = logging.getLogger(__name__)

HEADER_BG = "#1c2b4a"
HEADER_FG = "#f5ead9"
ACCENT = "#ff5a5f"
ACCENT_DARK = "#d94848"


def asset_path(name: str) -> Path | None:
    """Locate assets/icon.png both from source and from a PyInstaller bundle."""
    base = getattr(sys, "_MEIPASS", None)
    candidates = []
    if base:
        candidates.append(Path(base) / "assets" / name)
    candidates.append(Path(__file__).resolve().parent / "assets" / name)          # packaged copy
    candidates.append(Path(__file__).resolve().parent.parent / "assets" / name)   # repo checkout
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


class SettingsDialog(tk.Toplevel):
    def __init__(self, master: MangaReadApp):
        super().__init__(master)
        self.title("Settings")
        self.transient(master)
        self.resizable(False, False)
        self.settings = master.settings
        self.master_app = master

        body = ttk.Frame(self, padding=16)
        body.grid(row=0, column=0, sticky="nsew")

        ttk.Label(body, text="Request politeness", font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        ttk.Label(body, text="Delay between HTTP requests (seconds):").grid(row=1, column=0, sticky="w", pady=4)
        self.delay_var = tk.StringVar(value=f"{self.settings.delay:g}")
        ttk.Spinbox(body, from_=0.0, to=10.0, increment=0.05, width=8, textvariable=self.delay_var).grid(row=1, column=1, sticky="e", pady=4)

        ttk.Label(body, text="Timeout per request (seconds):").grid(row=2, column=0, sticky="w", pady=4)
        self.timeout_var = tk.StringVar(value=f"{self.settings.timeout:g}")
        ttk.Spinbox(body, from_=5, to=300, increment=5, width=8, textvariable=self.timeout_var).grid(row=2, column=1, sticky="e", pady=4)

        ttk.Label(body, text="Retries per request:").grid(row=3, column=0, sticky="w", pady=4)
        self.retries_var = tk.StringVar(value=str(self.settings.retries))
        ttk.Spinbox(body, from_=0, to=10, increment=1, width=8, textvariable=self.retries_var).grid(row=3, column=1, sticky="e", pady=4)

        ttk.Label(body, text="Behaviour", font=("TkDefaultFont", 10, "bold")).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(12, 6))

        self.skip_existing_var = tk.BooleanVar(value=self.settings.skip_existing)
        ttk.Checkbutton(body, text="Skip chapters whose .cbz already exists",
                        variable=self.skip_existing_var).grid(row=5, column=0, columnspan=2, sticky="w", pady=2)

        self.open_done_var = tk.BooleanVar(value=self.settings.open_folder_when_done)
        ttk.Checkbutton(body, text="Open the output folder when a batch finishes",
                        variable=self.open_done_var).grid(row=6, column=0, columnspan=2, sticky="w", pady=2)

        note = ttk.Label(body, text=("Every .cbz starts with the series cover image (000_cover)\n"
                                     "and carries ComicInfo.xml metadata."), foreground="#555555")
        note.grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save).grid(row=0, column=1)

        self.bind("<Return>", lambda _e: self._save())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.update_idletasks()
        self._center_on_parent()

    def _center_on_parent(self) -> None:
        parent = self.master_app
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _save(self) -> None:
        try:
            self.settings.delay = max(0.0, float(self.delay_var.get()))
            self.settings.timeout = max(5.0, float(self.timeout_var.get()))
            self.settings.retries = max(0, int(float(self.retries_var.get())))
        except ValueError:
            messagebox.showerror("Invalid value", "Delay, timeout and retries must be numbers.", parent=self)
            return
        self.settings.skip_existing = bool(self.skip_existing_var.get())
        self.settings.open_folder_when_done = bool(self.open_done_var.get())
        save_settings(self.settings)
        self.master_app.log_line("Settings saved.")
        self.destroy()


class MangaReadApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()

        self.title(f"{__app_name__} v{__version__}")
        self.minsize(960, 720)
        self.geometry("1100x780")
        self._apply_style()
        self._set_window_icon()

        # --- state
        self.mode = tk.StringVar(value=self.settings.mode if self.settings.mode in ("single", "bulk") else "bulk")
        self.manga: MangaInfo | None = None
        self.chapters: list[ChapterInfo] = []
        self.checked_ids: set[str] = set()
        self._busy = False           # a background task is running
        self._loading = False
        self.cancel_event = threading.Event()
        self.msg_queue: queue.Queue = queue.Queue()
        self._cover_photo = None     # keep PhotoImage reference alive
        self._chapter_urls: dict[str, ChapterInfo] = {}
        self._preferred_chapter: ChapterInfo | None = None  # chapter from a chapter-URL load

        self._build_menu()
        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_queue)
        self.log_line(f"Welcome to {__app_name__} v{__version__}")
        self.log_line("Paste a manga (or chapter) URL above and press Load.")

    # ------------------------------------------------------------ styling
    def _apply_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass  # fall back to whatever the platform provides
        style.configure("Accent.TButton", font=("TkDefaultFont", 10, "bold"), foreground="#ffffff",
                        background=ACCENT, borderwidth=0, focusthickness=0, padding=(14, 8))
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#caa3a3")])
        style.configure("AccentBig.TButton", font=("TkDefaultFont", 11, "bold"), foreground="#ffffff",
                        background=ACCENT, borderwidth=0, padding=(18, 10))
        style.map("AccentBig.TButton", background=[("active", ACCENT_DARK), ("disabled", "#caa3a3")])
        style.configure("Ghost.TButton", padding=(10, 6))
        style.configure("HeaderTitle.TLabel", background=HEADER_BG, foreground=HEADER_FG,
                        font=("TkDefaultFont", 17, "bold"))
        style.configure("HeaderSub.TLabel", background=HEADER_BG, foreground="#9fb0d0",
                        font=("TkDefaultFont", 9))
        style.configure("Treeview", rowheight=26)
        style.configure("Treeview.Heading", font=("TkDefaultFont", 9, "bold"))

    def _set_window_icon(self) -> None:
        icon = asset_path("icon.png")
        if icon is None:
            return
        try:
            photo = tk.PhotoImage(file=str(icon))
            self.iconphoto(True, photo)
            self._window_icon = photo  # keep alive
        except tk.TclError:
            pass

    # -------------------------------------------------------------- menu
    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Settings…", accelerator="Ctrl+,", command=self.open_settings)
        file_menu.add_command(label="Open output folder", command=self.open_output_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", accelerator="Ctrl+Q", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        tools_menu = tk.Menu(menubar, tearoff=False)
        tools_menu.add_command(label="Open config folder", command=self.open_config_folder)
        tools_menu.add_command(label="Clear log", command=self.clear_log)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.config(menu=menubar)
        self.bind_all("<Control-comma>", lambda _e: self.open_settings())
        self.bind_all("<Control-q>", lambda _e: self._on_close())

    # ------------------------------------------------------------ layout
    def _build_layout(self) -> None:
        # Header bar
        header = tk.Frame(self, bg=HEADER_BG, height=64)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        icon = asset_path("icon.png")
        if icon is not None:
            try:
                photo = tk.PhotoImage(file=str(icon)).subsample(4, 4)  # 256 -> 64
                tk.Label(header, image=photo, bg=HEADER_BG).pack(side="left", padx=(14, 10), pady=8)
                self._header_icon = photo
            except tk.TclError:
                pass
        title_box = tk.Frame(header, bg=HEADER_BG)
        title_box.pack(side="left", fill="y", pady=6)
        ttk.Label(title_box, text=__app_name__, style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(title_box, text=f"Download manhwa chapters from {__site__} as .cbz — with cover art",
                  style="HeaderSub.TLabel").pack(anchor="w")
        tk.Frame(self, bg=ACCENT, height=3).pack(fill="x", side="top")

        body = ttk.Frame(self, padding=(14, 10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)

        # --- 1. mode picker
        mode_frame = ttk.LabelFrame(body, text=" 1 · Download mode ", padding=(12, 8))
        mode_frame.grid(row=0, column=0, sticky="ew")
        ttk.Radiobutton(mode_frame,
                        text="Single Chapter  —  download exactly one chapter as a .cbz",
                        variable=self.mode, value="single", command=self._on_mode_changed).grid(row=0, column=0, sticky="w", padx=(4, 24))
        ttk.Radiobutton(mode_frame,
                        text="Bulk Download  —  download many/all chapters, each as its own .cbz",
                        variable=self.mode, value="bulk", command=self._on_mode_changed).grid(row=0, column=1, sticky="w")

        # --- 2. URL
        url_frame = ttk.LabelFrame(body, text=" 2 · Paste a manga or chapter URL ", padding=(12, 8))
        url_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        url_frame.columnconfigure(1, weight=1)
        ttk.Label(url_frame, text="URL:").grid(row=0, column=0, sticky="w", padx=(4, 8))
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(url_frame, textvariable=self.url_var, font=("TkDefaultFont", 10))
        self.url_entry.grid(row=0, column=1, sticky="ew", ipady=3)
        self.url_entry.bind("<Return>", lambda _e: self.load_manga())
        self.load_button = ttk.Button(url_frame, text="Load", style="Accent.TButton", command=self.load_manga)
        self.load_button.grid(row=0, column=2, padx=(8, 0))
        self.url_hint = ttk.Label(url_frame, text="e.g. https://www.mangaread.org/manga/hundred-years-game/",
                                  foreground="#777777")
        self.url_hint.grid(row=1, column=1, sticky="w", pady=(4, 0))

        # --- 3. info + chapter list
        middle = ttk.Frame(body)
        middle.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        middle.columnconfigure(1, weight=1)
        middle.rowconfigure(0, weight=1)

        info_frame = ttk.LabelFrame(middle, text=" Manga ", padding=(10, 8))
        info_frame.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        self.cover_label = ttk.Label(info_frame, text="No cover yet", anchor="center",
                                     relief="groove", width=22)
        self.cover_label.pack(pady=(2, 8), ipady=30)
        self.title_var = tk.StringVar(value="—")
        ttk.Label(info_frame, textvariable=self.title_var, font=("TkDefaultFont", 11, "bold"),
                  wraplength=180, justify="center").pack()
        self.meta_var = tk.StringVar(value="")
        ttk.Label(info_frame, textvariable=self.meta_var, foreground="#555555",
                  wraplength=180, justify="center", font=("TkDefaultFont", 8)).pack(pady=(4, 0))

        chapters_frame = ttk.LabelFrame(middle, text=" 3 · Select chapters ", padding=(10, 8))
        chapters_frame.grid(row=0, column=1, sticky="nsew")
        chapters_frame.rowconfigure(1, weight=1)
        chapters_frame.columnconfigure(0, weight=1)

        chapter_toolbar = ttk.Frame(chapters_frame)
        chapter_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.select_all_btn = ttk.Button(chapter_toolbar, text="Select all", style="Ghost.TButton",
                                         command=lambda: self._set_all_checked(True))
        self.select_all_btn.pack(side="left")
        self.select_none_btn = ttk.Button(chapter_toolbar, text="Select none", style="Ghost.TButton",
                                          command=lambda: self._set_all_checked(False))
        self.select_none_btn.pack(side="left", padx=(6, 0))
        self.invert_btn = ttk.Button(chapter_toolbar, text="Invert", style="Ghost.TButton",
                                     command=self._invert_checked)
        self.invert_btn.pack(side="left", padx=(6, 0))
        self.range_label = ttk.Label(chapter_toolbar, text="Range:")
        self.range_label.pack(side="left", padx=(14, 4))
        self.range_from_var = tk.StringVar(value="1")
        self.range_to_var = tk.StringVar(value="10")
        self.range_from_entry = ttk.Entry(chapter_toolbar, textvariable=self.range_from_var, width=5)
        self.range_from_entry.pack(side="left")
        self.range_to_label = ttk.Label(chapter_toolbar, text="to")
        self.range_to_label.pack(side="left", padx=4)
        self.range_to_entry = ttk.Entry(chapter_toolbar, textvariable=self.range_to_var, width=5)
        self.range_to_entry.pack(side="left")
        self.range_button = ttk.Button(chapter_toolbar, text="Select range", style="Ghost.TButton",
                                       command=self._select_range)
        self.range_button.pack(side="left", padx=(6, 0))
        self.selection_var = tk.StringVar(value="0 selected")
        ttk.Label(chapter_toolbar, textvariable=self.selection_var, foreground="#555555").pack(side="right")

        columns = ("check", "chapter", "release")
        self.chapter_tree = ttk.Treeview(chapters_frame, columns=columns, show="headings",
                                         selectmode="browse")
        self.chapter_tree.heading("check", text="✓")
        self.chapter_tree.heading("chapter", text="Chapter")
        self.chapter_tree.heading("release", text="Release date")
        self.chapter_tree.column("check", width=40, anchor="center", stretch=False)
        self.chapter_tree.column("chapter", width=320, anchor="w")
        self.chapter_tree.column("release", width=130, anchor="center", stretch=False)
        self.chapter_tree.grid(row=1, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(chapters_frame, orient="vertical", command=self.chapter_tree.yview)
        tree_scroll.grid(row=1, column=1, sticky="ns")
        self.chapter_tree.configure(yscrollcommand=tree_scroll.set)
        self.chapter_tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.chapter_tree.bind("<space>", self._on_tree_space)

        # --- 4. output folder
        output_frame = ttk.LabelFrame(body, text=" 4 · Output folder ", padding=(12, 8))
        output_frame.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        output_frame.columnconfigure(1, weight=1)
        ttk.Label(output_frame, text="Save to:").grid(row=0, column=0, sticky="w", padx=(4, 8))
        self.output_var = tk.StringVar(value=self.settings.output_dir)
        ttk.Entry(output_frame, textvariable=self.output_var).grid(row=0, column=1, sticky="ew", ipady=2)
        ttk.Button(output_frame, text="Browse…", command=self.browse_output).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(output_frame, text="Open folder", command=self.open_output_folder).grid(row=0, column=3, padx=(6, 0))

        # --- progress + actions
        progress_frame = ttk.Frame(body)
        progress_frame.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        progress_frame.columnconfigure(0, weight=1)

        self.overall_label_var = tk.StringVar(value="Overall: idle")
        ttk.Label(progress_frame, textvariable=self.overall_label_var).grid(row=0, column=0, sticky="w")
        self.overall_bar = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.overall_bar.grid(row=1, column=0, sticky="ew", pady=(2, 6))

        self.page_label_var = tk.StringVar(value="Current chapter: —")
        ttk.Label(progress_frame, textvariable=self.page_label_var).grid(row=2, column=0, sticky="w")
        self.page_bar = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.page_bar.grid(row=3, column=0, sticky="ew", pady=(2, 0))

        actions = ttk.Frame(body)
        actions.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        actions.columnconfigure(0, weight=1)
        self.download_button = ttk.Button(actions, text="Start Bulk Download", style="AccentBig.TButton",
                                          command=self.start_download, state="disabled")
        self.download_button.grid(row=0, column=0, sticky="ew")
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self.cancel_download,
                                        state="disabled", width=12)
        self.cancel_button.grid(row=0, column=1, padx=(10, 0), ipady=6)
        ttk.Button(actions, text="Settings…", command=self.open_settings).grid(row=0, column=2, padx=(10, 0), ipady=6)

        # --- log
        log_frame = ttk.LabelFrame(body, text=" Log ", padding=(8, 6))
        log_frame.grid(row=6, column=0, sticky="nsew", pady=(10, 0))
        body.rowconfigure(6, weight=1)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=7, wrap="word", state="disabled",
                                font=("TkFixedFont", 9), bg="#101725", fg="#d7e0f4",
                                insertbackground="#d7e0f4", relief="flat", padx=8, pady=6)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        # --- status bar
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w",
                  padding=(10, 3)).pack(fill="x", side="bottom")

        self._on_mode_changed()  # sync treeview columns with initial mode

    # ------------------------------------------------------------ helpers
    def log_line(self, message: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _on_mode_changed(self) -> None:
        mode = self.mode.get()
        self.settings.mode = mode
        bulk = mode == "bulk"
        display = ("check", "chapter", "release") if bulk else ("chapter", "release")
        self.chapter_tree.configure(displaycolumns=display)
        bulk_state = "normal" if bulk else "disabled"
        for widget in (self.select_all_btn, self.select_none_btn, self.invert_btn,
                       self.range_from_entry, self.range_to_entry, self.range_button):
            widget.configure(state=bulk_state)
        dim = "#888888" if not bulk else "#000000"
        self.range_label.configure(foreground=dim)
        self.range_to_label.configure(foreground=dim)
        self.download_button.configure(
            text="Start Bulk Download" if bulk else "Download Selected Chapter"
        )
        self._update_selection_label()

    # --------------------------------------------------------- load flow
    def load_manga(self) -> None:
        if self._busy:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Paste a mangaread.org manga or chapter URL first.", parent=self)
            return
        self._busy = True
        self._loading = True
        self._set_controls_running(True, loading=True)
        self.status_var.set("Loading manga info…")
        threading.Thread(target=self._load_worker, args=(url,), daemon=True, name="load").start()

    def _load_worker(self, url: str) -> None:
        try:
            kind, slug, segment = classify_url(url)
            scraper = MangaReadScraper(delay=self.settings.delay,
                                       timeout=self.settings.timeout,
                                       retries=self.settings.retries)
            manga = scraper.load_manga(manga_url_for(slug))
            chapter_url = normalize_url(url) if kind == "chapter" else None
            selected_chapter = None
            if kind == "chapter":
                selected_chapter = next(
                    (c for c in manga.chapters if c.url.rstrip("/").lower() == chapter_url.rstrip("/").lower()),
                    None,
                )
                if selected_chapter is None:
                    from .utils import parse_chapter_number
                    number = parse_chapter_number(segment or "")
                    selected_chapter = ChapterInfo(
                        title=f"Chapter {number:g}" if number is not None else (segment or "Chapter"),
                        url=chapter_url,
                        number=number,
                    )
                    manga.chapters = sorted([*manga.chapters, selected_chapter],
                                            key=lambda c: (c.number is None, c.number or 0.0, c.label))
            cover_bytes = None
            if PIL_AVAILABLE and manga.cover_url:
                try:
                    cover_bytes, _ctype = scraper.fetch_bytes_with_type(manga.cover_url)
                    if not looks_like_image(cover_bytes):
                        cover_bytes, _ctype = scraper.fetch_bytes_with_type(manga.cover_thumb_url or "")
                except Exception as exc:  # noqa: BLE001 - cover is cosmetic here; don't fail the load
                    log.info("Cover prefetch failed: %s", exc)
                    cover_bytes = None
            self.msg_queue.put(("manga_loaded", manga, kind, selected_chapter, cover_bytes))
        except ScraperError as exc:
            self.msg_queue.put(("load_error", str(exc)))
        except Exception as exc:  # unexpected: show it rather than dying silently
            log.exception("Load failed")
            self.msg_queue.put(("load_error", f"Unexpected error: {exc}"))

    def _on_manga_loaded(self, manga: MangaInfo, kind: str,
                         selected_chapter: ChapterInfo | None, cover_bytes: bytes | None) -> None:
        self.manga = manga
        self._preferred_chapter = selected_chapter if kind == "chapter" else None
        self.chapters = list(manga.chapters)
        self.title_var.set(manga.title)
        meta_bits = []
        if manga.authors:
            meta_bits.append("by " + ", ".join(manga.authors))
        if manga.artists and manga.artists != manga.authors:
            meta_bits.append("art: " + ", ".join(manga.artists))
        meta_bits.append(manga.status)
        if manga.rating:
            meta_bits.append(f"★ {manga.rating}")
        meta_bits.append(f"{manga.chapter_count} chapter(s)")
        self.meta_var.set("\n".join(meta_bits))

        self._show_cover(cover_bytes)
        self._populate_chapters()

        # Switch to single mode automatically when given a chapter URL.
        if kind == "chapter" and selected_chapter is not None:
            self.mode.set("single")
            self._on_mode_changed()
            for item_id, chapter in self._chapter_urls.items():
                if chapter.url.rstrip("/").lower() == selected_chapter.url.rstrip("/").lower():
                    self.chapter_tree.selection_set(item_id)
                    self.chapter_tree.see(item_id)
                    break
        else:
            self._set_all_checked(True)  # bulk: default to "everything checked"

        self.download_button.configure(state="normal")
        self.status_var.set(f"Loaded '{manga.title}' — {manga.chapter_count} chapter(s) available")
        self.log_line(f"Loaded: {manga.title} ({manga.chapter_count} chapters, status: {manga.status})")
        if kind == "chapter":
            self.log_line("Chapter URL detected — mode switched to Single Chapter.")

    def _show_cover(self, cover_bytes: bytes | None) -> None:
        if not PIL_AVAILABLE:
            self.cover_label.configure(text="(install Pillow\nfor cover preview)", image="")
            return
        if not cover_bytes:
            self.cover_label.configure(text="No cover\nfound", image="")
            return
        import io

        try:
            image = Image.open(io.BytesIO(cover_bytes))
            image.thumbnail((180, 270), Image.LANCZOS)
            self._cover_photo = ImageTk.PhotoImage(image)
            self.cover_label.configure(image=self._cover_photo, text="")
        except Exception as exc:  # noqa: BLE001 - preview is cosmetic
            log.info("Could not render cover preview: %s", exc)
            self.cover_label.configure(text="Cover preview\nunavailable", image="")

    def _populate_chapters(self) -> None:
        self.chapter_tree.delete(*self.chapter_tree.get_children())
        self._chapter_urls.clear()
        self.checked_ids.clear()
        for chapter in self.chapters:
            item_id = self.chapter_tree.insert(
                "", "end",
                values=("✔", chapter.label, chapter.release_date or "—"),
            )
            self._chapter_urls[item_id] = chapter
            self.checked_ids.add(item_id)
        self._update_selection_label()

    # ------------------------------------------------------ tree selection
    def _on_tree_click(self, event) -> None:
        if self.mode.get() != "bulk":
            return
        region = self.chapter_tree.identify_region(event.x, event.y)
        if region in ("cell", "tree"):
            item = self.chapter_tree.identify_row(event.y)
            if item:
                self._toggle_checked(item)
                return "break"

    def _on_tree_space(self, _event):
        if self.mode.get() == "bulk":
            for item in self.chapter_tree.selection():
                self._toggle_checked(item)
            return "break"

    def _toggle_checked(self, item_id: str) -> None:
        if item_id in self.checked_ids:
            self.checked_ids.discard(item_id)
        else:
            self.checked_ids.add(item_id)
        self.chapter_tree.set(item_id, "check", "✔" if item_id in self.checked_ids else "")
        self._update_selection_label()

    def _set_all_checked(self, value: bool) -> None:
        self.checked_ids = set(self._chapter_urls) if value else set()
        for item_id in self._chapter_urls:
            self.chapter_tree.set(item_id, "check", "✔" if value else "")
        self._update_selection_label()

    def _invert_checked(self) -> None:
        all_ids = set(self._chapter_urls)
        self.checked_ids = all_ids - self.checked_ids
        for item_id in all_ids:
            self.chapter_tree.set(item_id, "check", "✔" if item_id in self.checked_ids else "")
        self._update_selection_label()

    def _select_range(self) -> None:
        try:
            low = float(self.range_from_var.get())
            high = float(self.range_to_var.get())
        except ValueError:
            messagebox.showwarning("Range", "Range bounds must be numbers, e.g. 1 and 50.", parent=self)
            return
        if low > high:
            low, high = high, low
        self.checked_ids = set()
        for item_id, chapter in self._chapter_urls.items():
            if chapter.number is not None and low <= chapter.number <= high:
                self.checked_ids.add(item_id)
        for item_id in self._chapter_urls:
            self.chapter_tree.set(item_id, "check", "✔" if item_id in self.checked_ids else "")
        self._update_selection_label()

    def _update_selection_label(self) -> None:
        total = len(self._chapter_urls)
        if self.mode.get() == "single":
            self.selection_var.set(f"{total} chapter(s) listed")
        else:
            self.selection_var.set(f"{len(self.checked_ids)} of {total} selected")

    def _chapters_for_download(self) -> list[ChapterInfo]:
        if self.mode.get() == "single":
            selection = self.chapter_tree.selection()
            if selection:
                chapter = self._chapter_urls.get(selection[0])
                if chapter:
                    return [chapter]
            return [self._preferred_chapter] if self._preferred_chapter else []
        return [self._chapter_urls[item_id] for item_id in self._chapter_urls
                if item_id in self.checked_ids]

    # ------------------------------------------------------ download flow
    def start_download(self) -> None:
        if self._busy or self.manga is None:
            return
        chapters = self._chapters_for_download()
        if not chapters:
            if self.mode.get() == "single":
                messagebox.showwarning("Pick a chapter", "Select one chapter in the list first.", parent=self)
            else:
                messagebox.showwarning("Pick chapters", "Check at least one chapter to download.", parent=self)
            return
        output = Path(self.output_var.get()).expanduser()
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Output folder", f"Cannot use that folder:\n{exc}", parent=self)
            return

        self.settings.output_dir = str(output)
        save_settings(self.settings)

        self._busy = True
        self.cancel_event.clear()
        self._set_controls_running(True, loading=False)
        self.overall_bar["value"] = 0
        self.page_bar["value"] = 0
        self.status_var.set("Downloading…")
        threading.Thread(target=self._download_worker, args=(self.manga, chapters, output),
                         daemon=True, name="download").start()

    def _download_worker(self, manga: MangaInfo, chapters: list[ChapterInfo], output: Path) -> None:
        manager = DownloadManager(self.settings)

        def on_log(message: str) -> None:
            self.msg_queue.put(("log", message))

        def on_chapter_start(index: int, total: int, chapter: ChapterInfo) -> None:
            self.msg_queue.put(("chapter_start", index, total, chapter))

        def on_page_progress(done: int, total: int) -> None:
            self.msg_queue.put(("page_progress", done, total))

        def on_chapter_done(index: int, total: int, result) -> None:
            self.msg_queue.put(("chapter_done", index, total, result))

        callbacks = DownloadCallbacks(on_log=on_log, on_chapter_start=on_chapter_start,
                                      on_page_progress=on_page_progress, on_chapter_done=on_chapter_done)
        try:
            summary = manager.download_chapters(manga, chapters, output, self.cancel_event, callbacks)
            self.msg_queue.put(("finished", summary))
        except Exception as exc:
            log.exception("Download batch failed")
            self.msg_queue.put(("fatal_error", f"{exc}"))

    def cancel_download(self) -> None:
        if self._busy:
            self.cancel_event.set()
            self.log_line("Cancelling after the current page…")
            self.cancel_button.configure(state="disabled")

    # ------------------------------------------------------- message pump
    def _poll_queue(self) -> None:
        try:
            while True:
                message = self.msg_queue.get_nowait()
                self._handle_message(message)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_message(self, message) -> None:
        kind = message[0]
        if kind == "log":
            self.log_line(message[1])
        elif kind == "manga_loaded":
            _k, manga, url_kind, selected_chapter, cover_bytes = message
            self._busy = False
            self._loading = False
            self._on_manga_loaded(manga, url_kind, selected_chapter, cover_bytes)
            self._set_controls_running(False)
        elif kind == "load_error":
            self._busy = False
            self._loading = False
            self._set_controls_running(False)
            self.status_var.set("Load failed")
            self.log_line(f"ERROR: {message[1]}")
            messagebox.showerror("Could not load manga", message[1], parent=self)
        elif kind == "chapter_start":
            _k, index, total, chapter = message
            self.overall_label_var.set(f"Overall: chapter {index}/{total} — {chapter.label}")
            self.page_label_var.set(f"Current chapter: {chapter.label} (starting…)")
            self.page_bar["value"] = 0
            self.log_line(f"[{index}/{total}] {chapter.label}")
        elif kind == "page_progress":
            _k, done, total = message
            self.page_label_var.set(f"Current chapter: page {done}/{total}")
            self.page_bar["value"] = (done / max(total, 1)) * 100
        elif kind == "chapter_done":
            _k, index, total, result = message
            self.overall_bar["value"] = (index / max(total, 1)) * 100
            if result.ok:
                outcome = "skipped" if result.skipped else "done"
                self.log_line(f"  → {outcome}: {result.path.name if result.path else result.chapter.label}")
            else:
                self.log_line(f"  → FAILED: {result.error}")
            if result.warnings:
                for warning in result.warnings:
                    self.log_line(f"  warning: {warning}")
        elif kind == "finished":
            summary = message[1]
            self._busy = False
            self._set_controls_running(False)
            self.page_bar["value"] = 0
            self.page_label_var.set("Current chapter: —")
            text = (f"Done: {summary.succeeded} downloaded, {summary.skipped} skipped, "
                    f"{len(summary.failed)} failed.")
            if summary.cancelled:
                text = "Cancelled. " + text
            self.overall_label_var.set(text)
            self.status_var.set(text)
            self.log_line(text)
            if summary.failed:
                details = "\n".join(f"• {ch.label}: {err}" for ch, err in summary.failed[:10])
                messagebox.showwarning(
                    "Some chapters failed",
                    f"{len(summary.failed)} chapter(s) could not be downloaded:\n\n{details}\n\n"
                    "Check the log for details.", parent=self)
            else:
                messagebox.showinfo("Finished", text, parent=self)
            if self.settings.open_folder_when_done:
                self.open_output_folder()
        elif kind == "fatal_error":
            self._busy = False
            self._set_controls_running(False)
            self.status_var.set("Error")
            self.log_line(f"ERROR: {message[1]}")
            messagebox.showerror("Download failed", message[1], parent=self)

    def _set_controls_running(self, running: bool, loading: bool = False) -> None:
        state = "disabled" if running else "normal"
        self.url_entry.configure(state=state)
        self.load_button.configure(state=state)
        self.download_button.configure(state="disabled" if running or self.manga is None else "normal")
        self.cancel_button.configure(state="normal" if running and not loading else "disabled")
        for widget in (self.select_all_btn, self.select_none_btn, self.invert_btn):
            if self.mode.get() == "bulk":
                widget.configure(state=state)

    # ------------------------------------------------------------ output
    def browse_output(self) -> None:
        initial = self.output_var.get() or str(Path.home())
        chosen = filedialog.askdirectory(parent=self, initialdir=initial,
                                         title="Choose output folder")
        if chosen:
            self.output_var.set(chosen)

    def open_output_folder(self) -> None:
        path = Path(self.output_var.get()).expanduser()
        if not path.exists():
            messagebox.showinfo("Output folder", f"Folder does not exist yet:\n{path}", parent=self)
            return
        open_in_file_manager(path)

    def open_config_folder(self) -> None:
        open_in_file_manager(config_path().parent)

    # ----------------------------------------------------------- settings
    def open_settings(self) -> None:
        if self._busy:
            messagebox.showinfo("Busy", "Wait for the current task to finish.", parent=self)
            return
        SettingsDialog(self)

    # -------------------------------------------------------------- about
    def show_about(self) -> None:
        messagebox.showinfo(
            f"About {__app_name__}",
            f"{__app_name__} v{__version__}\n\n"
            f"Downloads chapters from {__site__} and saves them as .cbz\n"
            "files (each .cbz starts with the series cover image and\n"
            "carries ComicInfo.xml metadata).\n\n"
            "Works on Windows, Linux and SteamOS (Desktop Mode).\n\n"
            "For personal use. Respect the site's terms of service and\n"
            "the rights of the original authors.",
            parent=self,
        )

    # -------------------------------------------------------------- close
    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askyesno("Quit?", "A download is in progress. Cancel it and quit?",
                                       parent=self):
                return
            self.cancel_event.set()
        self.settings.output_dir = self.output_var.get()
        self.settings.mode = self.mode.get()
        save_settings(self.settings)
        self.destroy()


def launch_gui() -> int:
    app = MangaReadApp()
    app.mainloop()
    return 0
