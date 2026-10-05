"""smalldownloader.gui - the windowed front-end (Tkinter, no extra installs).

    python -m smalldownloader gui
    python -m smalldownloader gui -o ~/Downloads https://example.com/big.iso

Tkinter ships with the python.org installers on Windows/macOS and is a distro
package (``python3-tk``) on Linux, so the window stays dependency-free. The GUI
is only a view: every download runs in :class:`smalldownloader.core.DownloadManager`
and the window polls it a few times per second.
"""

from __future__ import annotations

import os
import subprocess
import sys
# Tkinter is imported on demand (see `_load_tkinter`) instead of at import time:
# the terminal mode then keeps working on machines without Tk, and this module
# can be merged into a single-file build that runs anywhere.
tk = None
ttk = None
tkfont = None
filedialog = None
messagebox = None
from typing import Dict, List, Optional

from . import __version__
from .curlimport import parse_curl
from .core import (
    CANCELLED,
    DONE,
    ERROR,
    PAUSED,
    PENDING,
    RUNNING,
    DownloadError,
    DownloadManager,
    Progress,
    default_download_dir,
    human_bytes,
    human_time,
)

POLL_MS = 300

# A restrained, system-font-friendly palette: light is the first-run default,
# while the built-in dark appearance keeps the same contrast and hierarchy.
# The window chrome and application menus remain native to the host OS.
BG = "#14161a"
PANEL = "#1c1f26"
PANEL_2 = "#22262f"
BORDER = "#2e3440"
FG = "#e6e9ef"
MUTED = "#8b93a7"
ACCENT = "#3b82f6"
ACCENT_HOVER = "#2f6fd0"
GREEN = "#4ade80"
RED = "#f87171"
YELLOW = "#fbbf24"
BLUE = "#60a5fa"

THEMES = {
    "light": {
        "bg": "#f3f5f9",
        "card": "#ffffff",
        "field": "#f8f9fc",
        "border": "#dce2eb",
        "text": "#1c2434",
        "muted": "#697586",
        "accent": "#2563eb",
        "accent_hover": "#1d4ed8",
        "selection": "#dbeafe",
        "green": "#15803d",
        "red": "#b42318",
        "yellow": "#a15c07",
        "blue": "#2563eb",
        "white": "#ffffff",
        "disabled": "#a7b0bf",
    },
    "dark": {
        "bg": "#101722",
        "card": "#182232",
        "field": "#111c2b",
        "border": "#2a394d",
        "text": "#e8eef7",
        "muted": "#94a3b8",
        "accent": "#4f8df7",
        "accent_hover": "#6aa0ff",
        "selection": "#2b4162",
        "green": "#4ade80",
        "red": "#f87171",
        "yellow": "#fbbf24",
        "blue": "#7db1ff",
        "white": "#ffffff",
        "disabled": "#66758a",
    },
}

STATE_COLORS = {
    PENDING: MUTED,
    RUNNING: BLUE,
    PAUSED: YELLOW,
    DONE: GREEN,
    ERROR: RED,
    CANCELLED: MUTED,
}

STATE_LABELS = {
    PENDING: "queued",
    RUNNING: "running",
    PAUSED: "paused",
    DONE: "done",
    ERROR: "error",
    CANCELLED: "cancelled",
}


def _open_path(path: str, reveal: bool = False) -> None:
    """Open a file/folder with the desktop's default handler."""
    if not path or not os.path.exists(path):
        return
    try:
        if os.name == "nt":
            if reveal:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path if not reveal else os.path.dirname(path)])
        else:
            subprocess.Popen(["xdg-open", path if not reveal else os.path.dirname(path)])
    except Exception:
        pass


def _load_tkinter() -> None:
    """Import Tkinter once, on demand. Raises ImportError when it is missing."""
    global tk, ttk, tkfont, filedialog, messagebox
    if tk is not None:
        return
    import tkinter as _tk
    from tkinter import filedialog as _filedialog
    from tkinter import font as _tkfont
    from tkinter import messagebox as _messagebox
    from tkinter import ttk as _ttk
    tk, ttk, tkfont = _tk, _ttk, _tkfont
    filedialog, messagebox = _filedialog, _messagebox


_APP_CLASS = None


def app_class():
    """Build (once) and return the window class, importing Tkinter on first use."""
    global _APP_CLASS
    if _APP_CLASS is not None:
        return _APP_CLASS
    _load_tkinter()

    class DownloaderApp(tk.Tk):
        """The whole window: URL box, queue table, log and controls."""

        def __init__(self, urls: Optional[List[str]] = None, outdir: Optional[str] = None) -> None:
            super().__init__()
            self.title("smalldownloader %s" % __version__)
            self.geometry("1120x760")
            self.minsize(880, 600)
            self.theme_name = "light"
            self.colors = dict(THEMES[self.theme_name])
            self._hand_cursor = "pointinghand" if sys.platform == "darwin" else "hand2"
            self._modifier = "Command" if sys.platform == "darwin" else "Control"
            self._shortcut_name = "⌘" if sys.platform == "darwin" else "Ctrl"
            self.configure(bg=self.colors["bg"])
            self.protocol("WM_DELETE_WINDOW", self._on_close)

            self.manager = DownloadManager(
                outdir=outdir or default_download_dir(),
                connections=4,
                jobs=2,
            )
            self._rows: Dict[int, str] = {}
            self._last_state: Dict[int, str] = {}
            self._progress_by_id = {}
            self._sort_column = None
            self._sort_reverse = False
            self._empty_state_visible = None
            self._closing = False

            self._build_style()
            self._build_menu()
            self._build_widgets()
            self._bind_shortcuts()

            for url in urls or []:
                self.url_text.insert("end", url + "\n")
            self.url_text.focus_set()
            self._poll()
            self._log("smalldownloader %s is ready. Add a link, then start your downloads."
                      % __version__)

        # ------------------------------------------------------------------ #
        # construction
        # ------------------------------------------------------------------ #

        def _build_style(self) -> None:
            """Install a crisp, native-feeling palette using only built-in ttk."""
            c = self.colors
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass

            ui_font = self._ui_font()
            mono_font = self._mono_font()
            style.configure(".", background=c["bg"], foreground=c["text"],
                            fieldbackground=c["field"], bordercolor=c["border"],
                            focuscolor=c["accent"], darkcolor=c["border"],
                            lightcolor=c["card"], troughcolor=c["field"],
                            font=(ui_font, 10))
            style.configure("TFrame", background=c["bg"])
            style.configure("App.TFrame", background=c["bg"])
            style.configure("Card.TFrame", background=c["card"], borderwidth=1,
                            bordercolor=c["border"], relief="solid", padding=0)
            style.configure("CardInner.TFrame", background=c["card"])
            style.configure("Empty.TFrame", background=c["card"])
            style.configure("TLabel", background=c["bg"], foreground=c["text"],
                            font=(ui_font, 10))
            style.configure("Card.TLabel", background=c["card"], foreground=c["text"])
            style.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"])
            style.configure("CardMuted.TLabel", background=c["card"], foreground=c["muted"])
            style.configure("Eyebrow.TLabel", background=c["card"], foreground=c["muted"],
                            font=(ui_font, 8, "bold"))
            style.configure("BrandMark.TLabel", background=c["accent"], foreground=c["white"],
                            font=(ui_font, 17, "bold"), padding=(9, 4))
            style.configure("Head.TLabel", background=c["bg"], foreground=c["text"],
                            font=(ui_font, 17, "bold"))
            style.configure("Subhead.TLabel", background=c["bg"], foreground=c["muted"],
                            font=(ui_font, 9))
            style.configure("Section.TLabel", background=c["card"], foreground=c["text"],
                            font=(ui_font, 12, "bold"))
            style.configure("StatValue.TLabel", background=c["card"], foreground=c["text"],
                            font=(ui_font, 15, "bold"))
            style.configure("StatCaption.TLabel", background=c["card"], foreground=c["muted"],
                            font=(ui_font, 8, "bold"))
            style.configure("EmptyTitle.TLabel", background=c["card"], foreground=c["text"],
                            font=(ui_font, 12, "bold"))
            style.configure("EmptyCopy.TLabel", background=c["card"], foreground=c["muted"],
                            font=(ui_font, 9))
            style.configure("Ready.TLabel", background=c["bg"], foreground=c["green"],
                            font=(ui_font, 9, "bold"))
            style.configure("ActiveStatus.TLabel", background=c["bg"], foreground=c["blue"],
                            font=(ui_font, 9, "bold"))
            style.configure("WarningStatus.TLabel", background=c["bg"], foreground=c["yellow"],
                            font=(ui_font, 9, "bold"))
            style.configure("ErrorStatus.TLabel", background=c["bg"], foreground=c["red"],
                            font=(ui_font, 9, "bold"))
            style.configure("Footer.TLabel", background=c["bg"], foreground=c["muted"],
                            font=(ui_font, 8))

            style.configure("TButton", background=c["field"], foreground=c["text"],
                            borderwidth=1, bordercolor=c["border"], focusthickness=2,
                            focuscolor=c["accent"], padding=(11, 7), font=(ui_font, 9, "bold"))
            style.map("TButton",
                      background=[("disabled", c["bg"]), ("pressed", c["selection"]),
                                  ("active", c["selection"])],
                      foreground=[("disabled", c["disabled"])])
            style.configure("Primary.TButton", background=c["accent"], foreground=c["white"],
                            borderwidth=0, padding=(14, 8), font=(ui_font, 9, "bold"))
            style.map("Primary.TButton",
                      background=[("disabled", c["disabled"]), ("pressed", c["accent_hover"]),
                                  ("active", c["accent_hover"])],
                      foreground=[("disabled", c["white"])])
            style.configure("Secondary.TButton", background=c["card"], foreground=c["text"],
                            borderwidth=1, bordercolor=c["border"], padding=(11, 7))
            style.map("Secondary.TButton",
                      background=[("disabled", c["bg"]), ("active", c["selection"])],
                      foreground=[("disabled", c["disabled"])])
            style.configure("Quiet.TButton", background=c["bg"], foreground=c["muted"],
                            borderwidth=0, padding=(9, 6))
            style.map("Quiet.TButton", background=[("active", c["selection"])],
                      foreground=[("active", c["text"])])
            style.configure("Small.TButton", background=c["card"], foreground=c["muted"],
                            borderwidth=0, padding=(7, 4), font=(ui_font, 8, "bold"))
            style.map("Small.TButton", background=[("active", c["selection"])],
                      foreground=[("active", c["text"])])
            style.configure("TEntry", fieldbackground=c["field"], foreground=c["text"],
                            insertcolor=c["text"], bordercolor=c["border"], padding=(9, 7))
            style.map("TEntry", bordercolor=[("focus", c["accent"])])
            style.configure("TSpinbox", fieldbackground=c["field"], foreground=c["text"],
                            arrowcolor=c["muted"], bordercolor=c["border"], padding=(5, 5))
            style.configure("Treeview", background=c["card"], fieldbackground=c["card"],
                            foreground=c["text"], rowheight=31, borderwidth=0,
                            font=(mono_font, 9))
            style.configure("Treeview.Heading", background=c["field"], foreground=c["muted"],
                            relief="flat", borderwidth=0, padding=(9, 8),
                            font=(ui_font, 8, "bold"))
            style.map("Treeview.Heading", background=[("active", c["selection"])])
            style.map("Treeview", background=[("selected", c["selection"])],
                      foreground=[("selected", c["text"])])
            style.configure("TNotebook", background=c["card"], borderwidth=0,
                            tabmargins=(0, 0, 0, 0))
            style.configure("TNotebook.Tab", background=c["field"], foreground=c["muted"],
                            padding=(13, 7), font=(ui_font, 8, "bold"))
            style.map("TNotebook.Tab", background=[("selected", c["card"])],
                      foreground=[("selected", c["text"])])
            style.configure("Vertical.TScrollbar", background=c["field"],
                            troughcolor=c["card"], bordercolor=c["card"], arrowcolor=c["muted"])
            style.configure("Horizontal.TScrollbar", background=c["field"],
                            troughcolor=c["card"], bordercolor=c["card"], arrowcolor=c["muted"])
            style.configure("TProgressbar", background=c["accent"], troughcolor=c["field"],
                            borderwidth=0)

            self._state_colors = {
                PENDING: c["muted"],
                RUNNING: c["blue"],
                PAUSED: c["yellow"],
                DONE: c["green"],
                ERROR: c["red"],
                CANCELLED: c["muted"],
            }

        def _ui_font(self) -> str:
            return self._pick_font(("Segoe UI", "SF Pro Text", "Helvetica Neue",
                                    "DejaVu Sans", "Arial"))

        def _mono_font(self) -> str:
            return self._pick_font(("Cascadia Code", "Consolas", "Menlo",
                                    "DejaVu Sans Mono", "Courier New", "Courier"))

        def _pick_font(self, candidates) -> str:
            try:
                available = {name.lower() for name in tkfont.families(self)}
            except tk.TclError:
                return "TkDefaultFont"
            for name in candidates:
                if name.lower() in available:
                    return name
            return "TkDefaultFont"

        def _menu_options(self) -> dict:
            c = self.colors
            return {
                "bg": c["card"],
                "fg": c["text"],
                "activebackground": c["selection"],
                "activeforeground": c["text"],
                "disabledforeground": c["muted"],
                "bd": 0,
                "tearoff": 0,
            }

        def _make_menu(self, parent):
            try:
                menu = tk.Menu(parent, **self._menu_options())
            except tk.TclError:  # Aqua may keep menu colours under system control.
                menu = tk.Menu(parent, tearoff=0)
            self._menus.append(menu)
            return menu

        def _build_menu(self) -> None:
            self._menus = []
            mod = self._shortcut_name
            menubar = self._make_menu(self)
            file_menu = self._make_menu(menubar)
            file_menu.add_command(label="Add URLs from file…", accelerator="%s+O" % mod,
                                  command=self._add_from_file)
            file_menu.add_command(label="Import from browser cURL…",
                                  accelerator="%s+Shift+O" % mod,
                                  command=self._open_curl_dialog)
            file_menu.add_separator()
            file_menu.add_command(label="Choose download folder…", command=self._browse_dir)
            file_menu.add_separator()
            file_menu.add_command(label="Quit", accelerator="%s+Q" % mod,
                                  command=self._on_close)
            menubar.add_cascade(label="File", menu=file_menu)

            queue_menu = self._make_menu(menubar)
            queue_menu.add_command(label="Start downloads", accelerator="F5",
                                   command=self._start)
            queue_menu.add_command(label="Add links to queue",
                                   accelerator="%s+Return" % mod, command=self._add_urls)
            queue_menu.add_separator()
            queue_menu.add_command(label="Pause all", command=self._pause)
            queue_menu.add_command(label="Resume all", command=self._resume)
            queue_menu.add_command(label="Cancel all", command=self._cancel)
            queue_menu.add_separator()
            queue_menu.add_command(label="Retry failed", command=self._retry_failed)
            queue_menu.add_command(label="Remove finished", command=self._clear_finished)
            menubar.add_cascade(label="Queue", menu=queue_menu)

            view_menu = self._make_menu(menubar)
            view_menu.add_command(label="Toggle light / dark appearance",
                                   command=self._toggle_theme)
            menubar.add_cascade(label="View", menu=view_menu)

            help_menu = self._make_menu(menubar)
            help_menu.add_command(label="About smalldownloader", command=self._about)
            menubar.add_cascade(label="Help", menu=help_menu)
            self.configure(menu=menubar)

        def _build_widgets(self) -> None:
            c = self.colors
            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(4, weight=1, minsize=150)

            # -- brand bar -------------------------------------------------- #
            header = ttk.Frame(self, style="App.TFrame")
            header.grid(row=0, column=0, sticky="ew", padx=18, pady=(13, 7))
            header.grid_columnconfigure(2, weight=1)
            ttk.Label(header, text="S", style="BrandMark.TLabel", anchor="center",
                      width=2).grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 11))
            ttk.Label(header, text="smalldownloader", style="Head.TLabel").grid(
                row=0, column=1, sticky="sw")
            ttk.Label(header, text="Reliable downloads, made simple.",
                      style="Subhead.TLabel").grid(row=1, column=1, sticky="nw", pady=(1, 0))
            self.status_label = ttk.Label(header, text="●  Ready", style="Ready.TLabel")
            self.status_label.grid(row=0, column=3, rowspan=2, sticky="e", padx=(8, 12))
            self.theme_button = self._button(
                header, "Dark theme", self._toggle_theme, style="Quiet.TButton")
            self.theme_button.grid(row=0, column=4, rowspan=2, sticky="e")

            # -- at-a-glance metrics --------------------------------------- #
            stats = ttk.Frame(self, style="App.TFrame")
            stats.grid(row=1, column=0, sticky="ew", padx=18, pady=5)
            for column in range(4):
                stats.grid_columnconfigure(column, weight=1, uniform="stats")
            self.stat_vars = {
                "active": tk.StringVar(value="0"),
                "queued": tk.StringVar(value="0"),
                "completed": tk.StringVar(value="0"),
                "speed": tk.StringVar(value="0 B/s"),
            }
            stat_specs = (
                ("active", "DOWNLOADING"),
                ("queued", "WAITING"),
                ("completed", "COMPLETED"),
                ("speed", "TRANSFER RATE"),
            )
            for index, (key, caption) in enumerate(stat_specs):
                card = ttk.Frame(stats, style="Card.TFrame", padding=(13, 8))
                card.grid(row=0, column=index, sticky="ew",
                          padx=(0 if index == 0 else 5, 0 if index == 3 else 5))
                ttk.Label(card, text=caption, style="StatCaption.TLabel").pack(anchor="w")
                ttk.Label(card, textvariable=self.stat_vars[key], style="StatValue.TLabel").pack(
                    anchor="w", pady=(2, 0))

            # -- destination and performance settings ---------------------- #
            settings = ttk.Frame(self, style="Card.TFrame", padding=(13, 9))
            settings.grid(row=2, column=0, sticky="ew", padx=18, pady=5)
            settings.grid_columnconfigure(1, weight=1)
            ttk.Label(settings, text="SAVE TO", style="Eyebrow.TLabel").grid(
                row=0, column=0, sticky="w", padx=(0, 9))
            self.dir_var = tk.StringVar(value=self.manager.outdir)
            self.dir_entry = ttk.Entry(settings, textvariable=self.dir_var, cursor="xterm")
            self.dir_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
            self._button(settings, "Browse…", self._browse_dir,
                         style="Secondary.TButton").grid(row=0, column=2, padx=(0, 16))

            ttk.Label(settings, text="CONNECTIONS / FILE", style="Eyebrow.TLabel").grid(
                row=0, column=3, sticky="w", padx=(0, 7))
            self.conn_var = tk.StringVar(value="4")
            ttk.Spinbox(settings, from_=1, to=32, width=4, textvariable=self.conn_var,
                        cursor="xterm").grid(row=0, column=4, sticky="w", padx=(0, 16))

            ttk.Label(settings, text="FILES AT ONCE", style="Eyebrow.TLabel").grid(
                row=0, column=5, sticky="w", padx=(0, 7))
            self.jobs_var = tk.StringVar(value="2")
            ttk.Spinbox(settings, from_=1, to=16, width=4, textvariable=self.jobs_var,
                        cursor="xterm").grid(row=0, column=6, sticky="w")

            # -- new-download composer ------------------------------------- #
            composer = ttk.Frame(self, style="Card.TFrame", padding=(13, 10))
            composer.grid(row=3, column=0, sticky="ew", padx=18, pady=5)
            composer.grid_columnconfigure(0, weight=1)
            ttk.Label(composer, text="Add downloads", style="Section.TLabel").grid(
                row=0, column=0, sticky="sw")
            ttk.Label(
                composer, text="Paste direct links — one per line. Multiple links are welcome.",
                style="CardMuted.TLabel",
            ).grid(row=1, column=0, sticky="nw", pady=(1, 0))
            self._button(composer, "Import cURL…", self._open_curl_dialog,
                         style="Quiet.TButton").grid(row=0, column=1, rowspan=2, sticky="ne")

            url_input = ttk.Frame(composer, style="CardInner.TFrame")
            url_input.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(9, 7))
            url_input.grid_columnconfigure(0, weight=1)
            self.url_text = tk.Text(
                url_input, height=3, bg=c["field"], fg=c["text"],
                insertbackground=c["text"], selectbackground=c["selection"],
                selectforeground=c["text"], relief="flat", highlightthickness=1,
                highlightbackground=c["border"], highlightcolor=c["accent"],
                wrap="char", font=(self._mono_font(), 10), padx=10, pady=7,
                cursor="xterm", undo=True,
            )
            self.url_text.grid(row=0, column=0, sticky="ew")
            url_scroll = ttk.Scrollbar(url_input, orient="vertical", command=self.url_text.yview,
                                       style="Vertical.TScrollbar")
            url_scroll.grid(row=0, column=1, sticky="ns")
            self.url_text.configure(yscrollcommand=url_scroll.set)

            ttk.Label(
                composer,
                text="%s+Enter to add  ·  %s+Shift+Enter to start" % (
                    self._shortcut_name, self._shortcut_name),
                style="CardMuted.TLabel",
            ).grid(row=3, column=0, sticky="w")
            composer_actions = ttk.Frame(composer, style="CardInner.TFrame")
            composer_actions.grid(row=3, column=1, sticky="e")
            self.paste_button = self._button(composer_actions, "Paste", self._paste_urls,
                                             style="Quiet.TButton")
            self.paste_button.pack(side="left", padx=(0, 2))
            self.add_button = self._button(composer_actions, "Add to queue", self._add_urls,
                                           style="Secondary.TButton")
            self.add_button.pack(side="left", padx=(2, 6))
            self.start_button = self._button(composer_actions, "Start downloads", self._start,
                                             style="Primary.TButton")
            self.start_button.pack(side="left")

            # -- queue and bulk controls ----------------------------------- #
            queue_panel = ttk.Frame(self, style="Card.TFrame", padding=(11, 8))
            queue_panel.grid(row=4, column=0, sticky="nsew", padx=18, pady=5)
            queue_panel.grid_columnconfigure(0, weight=1)
            queue_panel.grid_rowconfigure(1, weight=1)
            queue_header = ttk.Frame(queue_panel, style="CardInner.TFrame")
            queue_header.grid(row=0, column=0, sticky="ew")
            queue_header.grid_columnconfigure(1, weight=1)
            ttk.Label(queue_header, text="Download queue", style="Section.TLabel").grid(
                row=0, column=0, sticky="w")
            self.queue_count_var = tk.StringVar(value="0 downloads")
            ttk.Label(queue_header, textvariable=self.queue_count_var,
                      style="CardMuted.TLabel").grid(row=0, column=1, sticky="w", padx=(9, 0))
            bulk_actions = ttk.Frame(queue_header, style="CardInner.TFrame")
            bulk_actions.grid(row=0, column=2, sticky="e")
            self.pause_button = self._button(bulk_actions, "Pause", self._pause,
                                             style="Small.TButton")
            self.pause_button.pack(side="left", padx=1)
            self.resume_button = self._button(bulk_actions, "Resume", self._resume,
                                              style="Small.TButton")
            self.resume_button.pack(side="left", padx=1)
            self.cancel_button = self._button(bulk_actions, "Cancel all", self._cancel,
                                              style="Small.TButton")
            self.cancel_button.pack(side="left", padx=1)
            self.retry_button = self._button(bulk_actions, "Retry failed", self._retry_failed,
                                             style="Small.TButton")
            self.retry_button.pack(side="left", padx=1)
            self.clear_button = self._button(bulk_actions, "Clear finished", self._clear_finished,
                                             style="Small.TButton")
            self.clear_button.pack(side="left", padx=1)

            table_wrap = ttk.Frame(queue_panel, style="CardInner.TFrame")
            table_wrap.grid(row=1, column=0, sticky="nsew", pady=(7, 0))
            table_wrap.grid_rowconfigure(0, weight=1)
            table_wrap.grid_columnconfigure(0, weight=1)
            columns = ("num", "name", "progress", "done", "speed", "eta", "status")
            self._heading_labels = {
                "num": "NO.", "name": "FILE", "progress": "PROGRESS", "done": "SIZE",
                "speed": "SPEED", "eta": "ETA", "status": "STATUS",
            }
            self.tree = ttk.Treeview(table_wrap, columns=columns, show="headings",
                                     selectmode="extended")
            self._set_pointer_cursor(self.tree)
            widths = {"num": 43, "name": 250, "progress": 174, "done": 118,
                      "speed": 102, "eta": 78, "status": 160}
            minimums = {"num": 36, "name": 145, "progress": 135, "done": 86,
                        "speed": 75, "eta": 62, "status": 105}
            anchors = {"num": "center", "progress": "w", "done": "e", "speed": "e",
                       "eta": "e"}
            for column in columns:
                self.tree.heading(
                    column, text=self._heading_labels[column],
                    command=lambda selected=column: self._sort_by(selected),
                )
                self.tree.column(
                    column, width=widths[column], minwidth=minimums[column],
                    anchor=anchors.get(column, "w"), stretch=column in ("name", "status"),
                )
            self.tree.grid(row=0, column=0, sticky="nsew")
            tree_scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview,
                                        style="Vertical.TScrollbar")
            tree_scroll.grid(row=0, column=1, rowspan=2, sticky="ns")
            horizontal_scroll = ttk.Scrollbar(
                table_wrap, orient="horizontal", command=self.tree.xview,
                style="Horizontal.TScrollbar",
            )
            horizontal_scroll.grid(row=1, column=0, sticky="ew")
            self.tree.configure(yscrollcommand=tree_scroll.set,
                                xscrollcommand=horizontal_scroll.set)
            for state, color in self._state_colors.items():
                self.tree.tag_configure(state, foreground=color)

            self.tree.bind("<Double-1>", self._on_double_click)
            self.tree.bind("<Button-3>", self._on_right_click)
            self.tree.bind("<Button-2>", self._on_right_click)
            self.tree.bind("<Delete>", lambda _event: self._remove_selected())
            self.tree.bind("<Return>", lambda _event: self._open_selected())
            self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_details())

            self.empty_state = ttk.Frame(table_wrap, style="Empty.TFrame", padding=(16, 10))
            ttk.Label(self.empty_state, text="Your queue is clear",
                      style="EmptyTitle.TLabel").pack()
            ttk.Label(self.empty_state,
                      text="Add a link above and your downloads will appear here.",
                      style="EmptyCopy.TLabel").pack(pady=(3, 0))
            self._show_empty_queue(True)

            # -- activity and selected-download details -------------------- #
            activity = ttk.Frame(self, style="Card.TFrame", padding=(9, 5))
            activity.grid(row=5, column=0, sticky="nsew", padx=18, pady=5)
            activity.grid_columnconfigure(0, weight=1)
            activity.grid_rowconfigure(0, weight=1)
            self.notebook = ttk.Notebook(activity)
            self.notebook.grid(row=0, column=0, sticky="nsew")

            log_tab = ttk.Frame(self.notebook, style="CardInner.TFrame")
            log_tab.grid_rowconfigure(0, weight=1)
            log_tab.grid_columnconfigure(0, weight=1)
            self.log_text = tk.Text(
                log_tab, height=5, bg=c["card"], fg=c["text"],
                insertbackground=c["text"], selectbackground=c["selection"],
                selectforeground=c["text"], relief="flat", highlightthickness=0,
                wrap="word", font=(self._mono_font(), 9), padx=10, pady=7,
                cursor="xterm",
            )
            self.log_text.grid(row=0, column=0, sticky="nsew")
            log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log_text.yview,
                                       style="Vertical.TScrollbar")
            log_scroll.grid(row=0, column=1, sticky="ns")
            self.log_text.configure(yscrollcommand=log_scroll.set, state="disabled")
            self.notebook.add(log_tab, text="Activity")

            info_tab = ttk.Frame(self.notebook, style="CardInner.TFrame")
            info_tab.grid_rowconfigure(0, weight=1)
            info_tab.grid_columnconfigure(0, weight=1)
            self.detail_var = tk.StringVar(value="Select a download to see its details.")
            ttk.Label(info_tab, textvariable=self.detail_var, justify="left", anchor="nw",
                      wraplength=1000, font=(self._mono_font(), 9),
                      style="Card.TLabel").grid(row=0, column=0, sticky="nsew",
                                                padx=12, pady=10)
            self.notebook.add(info_tab, text="Details")

            # -- quiet, useful footer -------------------------------------- #
            footer = ttk.Frame(self, style="App.TFrame")
            footer.grid(row=6, column=0, sticky="ew", padx=19, pady=(1, 8))
            footer.grid_columnconfigure(0, weight=1)
            ttk.Label(footer, text="Tip: double-click a completed item to open it  ·  "
                      "Right-click a download for more actions.",
                      style="Footer.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Label(footer, text="v%s" % __version__, style="Footer.TLabel").grid(
                row=0, column=1, sticky="e")

            self._build_context_menu()

        def _button(self, parent, text: str, command, style: str = "TButton", **kwargs):
            """Create a keyboard-focusable button with a platform pointer cursor."""
            options = dict(text=text, command=command, style=style, takefocus=True)
            options.update(kwargs)
            for cursor in (self._hand_cursor, "hand2", "pointinghand"):
                try:
                    return ttk.Button(parent, cursor=cursor, **options)
                except tk.TclError:
                    continue
            return ttk.Button(parent, **options)

        def _set_pointer_cursor(self, widget) -> None:
            for cursor in (self._hand_cursor, "hand2", "pointinghand"):
                try:
                    widget.configure(cursor=cursor)
                    return
                except tk.TclError:
                    continue

        def _show_empty_queue(self, visible: bool) -> None:
            if self._empty_state_visible == visible:
                return
            self._empty_state_visible = visible
            if visible:
                self.empty_state.place(relx=0.5, rely=0.5, anchor="center")
            else:
                self.empty_state.place_forget()

        def _bind_shortcuts(self) -> None:
            mod = self._modifier
            self.bind_all("<%s-o>" % mod, self._shortcut_add_from_file)
            self.bind_all("<%s-Shift-o>" % mod, self._shortcut_curl_import)
            self.bind_all("<%s-Return>" % mod, self._shortcut_add)
            self.bind_all("<%s-Shift-Return>" % mod, self._shortcut_start)
            self.bind_all("<F5>", self._shortcut_start)
            self.bind_all("<%s-q>" % mod, self._shortcut_quit)

        def _shortcut_add_from_file(self, _event=None):
            self._add_from_file()
            return "break"

        def _shortcut_curl_import(self, _event=None):
            self._open_curl_dialog()
            return "break"

        def _shortcut_add(self, _event=None):
            self._add_urls()
            return "break"

        def _shortcut_start(self, _event=None):
            self._start()
            return "break"

        def _shortcut_quit(self, _event=None):
            self._on_close()
            return "break"

        def _toggle_theme(self) -> None:
            self.theme_name = "dark" if self.theme_name == "light" else "light"
            self.colors = dict(THEMES[self.theme_name])
            self._build_style()
            self.configure(bg=self.colors["bg"])
            self.theme_button.configure(
                text="%s theme" % ("Dark" if self.theme_name == "light" else "Light"))
            self.url_text.configure(
                bg=self.colors["field"], fg=self.colors["text"],
                insertbackground=self.colors["text"],
                selectbackground=self.colors["selection"],
                selectforeground=self.colors["text"],
                highlightbackground=self.colors["border"],
                highlightcolor=self.colors["accent"],
            )
            self.log_text.configure(
                bg=self.colors["card"], fg=self.colors["text"],
                insertbackground=self.colors["text"],
                selectbackground=self.colors["selection"],
                selectforeground=self.colors["text"],
            )
            for state, color in self._state_colors.items():
                self.tree.tag_configure(state, foreground=color)
            for menu in self._menus:
                try:
                    menu.configure(**self._menu_options())
                except tk.TclError:
                    pass
            self._log("Switched to %s appearance." % self.theme_name)

        def _build_context_menu(self) -> None:
            self.context_menu = self._make_menu(self)
            self.context_menu.add_command(label="Pause", command=self._pause_selected)
            self.context_menu.add_command(label="Resume", command=self._resume_selected)
            self.context_menu.add_command(label="Cancel", command=self._cancel_selected)
            self.context_menu.add_command(label="Retry", command=self._retry_selected)
            self.context_menu.add_separator()
            self.context_menu.add_command(label="Open file", command=self._open_selected)
            self.context_menu.add_command(label="Show in folder", command=self._reveal_selected)
            self.context_menu.add_command(label="Copy URL", command=self._copy_selected_url)
            self.context_menu.add_separator()
            self.context_menu.add_command(label="Remove from list", command=self._remove_selected)

        def _sort_by(self, column: str) -> None:
            """Sort the visible queue by the selected heading."""
            if self._sort_column == column:
                self._sort_reverse = not self._sort_reverse
            else:
                self._sort_column = column
                self._sort_reverse = False
            self._update_sort_headings()
            self._sort_rows()

        def _sort_key(self, task_id: int, column: str):
            state = self._progress_by_id.get(task_id)
            if state is None:
                return (1, task_id)
            if column == "num":
                value = state.id
            elif column == "name":
                value = state.name.casefold()
            elif column == "progress":
                value = (state.total <= 0, state.percent if state.percent >= 0 else -1)
            elif column == "done":
                value = state.downloaded
            elif column == "speed":
                value = state.speed
            elif column == "eta":
                value = (state.eta is None, state.eta or 0)
            elif column == "status":
                order = {RUNNING: 0, PENDING: 1, PAUSED: 2, ERROR: 3,
                         CANCELLED: 4, DONE: 5}
                value = (order.get(state.state, 9), state.state)
            else:
                value = state.id
            return (0, value, state.id)

        def _sort_rows(self) -> None:
            if not self._sort_column:
                return
            rows = []
            for row in self.tree.get_children(""):
                try:
                    task_id = int(row)
                except (TypeError, ValueError):
                    continue
                rows.append((self._sort_key(task_id, self._sort_column), row))
            rows.sort(key=lambda item: item[0], reverse=self._sort_reverse)
            for index, (_key, row) in enumerate(rows):
                self.tree.move(row, "", index)

        def _update_sort_headings(self) -> None:
            for column, label in self._heading_labels.items():
                marker = "  %s" % ("▼" if self._sort_reverse else "▲") \
                    if column == self._sort_column else ""
                self.tree.heading(column, text=label + marker,
                                 command=lambda selected=column: self._sort_by(selected))

        # ------------------------------------------------------------------ #
        # helpers
        # ------------------------------------------------------------------ #

        def _log(self, message: str) -> None:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        def _task(self, task_id) -> Optional[object]:
            try:
                task_id = int(task_id)
            except (TypeError, ValueError):
                return None
            for task in self.manager.tasks:
                if task.id == task_id:
                    return task
            return None

        def _selected_tasks(self) -> List[object]:
            return [task for task in (self._task(iid) for iid in self.tree.selection()) if task]

        def _apply_settings(self) -> None:
            self.manager.outdir = os.path.abspath(os.path.expanduser(
                self.dir_var.get().strip() or default_download_dir()))
            try:
                connections = int(float(self.conn_var.get()))
            except (TypeError, ValueError, OverflowError):
                connections = 4
            try:
                jobs = int(float(self.jobs_var.get()))
            except (TypeError, ValueError, OverflowError):
                jobs = 2
            self.manager.connections = min(32, max(1, connections))
            self.manager.jobs = min(16, max(1, jobs))
            self.conn_var.set(str(self.manager.connections))
            self.jobs_var.set(str(self.manager.jobs))
            self.dir_var.set(self.manager.outdir)

        # ------------------------------------------------------------------ #
        # actions
        # ------------------------------------------------------------------ #

        def _browse_dir(self) -> None:
            initialdir = self.dir_var.get() or os.path.expanduser("~")
            chosen = filedialog.askdirectory(initialdir=initialdir)
            if chosen:
                self.dir_var.set(chosen)
                self.dir_entry.focus_set()

        def _paste_urls(self) -> None:
            try:
                contents = self.clipboard_get()
            except tk.TclError:
                self._log("Clipboard is empty or does not contain text.")
                self.url_text.focus_set()
                return
            if not contents.strip():
                self._log("Clipboard is empty — copy a download link first.")
                self.url_text.focus_set()
                return
            self.url_text.insert("insert", contents.strip() + "\n")
            self.url_text.focus_set()

        def _add_from_file(self) -> None:
            path = filedialog.askopenfilename(
                title="Pick a text file with one URL per line",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
            if not path:
                return
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    lines = [line.strip() for line in handle
                             if line.strip() and not line.strip().startswith("#")]
            except OSError as exc:
                messagebox.showerror("smalldownloader", "Cannot read %s:\n%s" % (path, exc))
                return
            self._queue_urls(lines)

        def _add_urls(self) -> None:
            raw = self.url_text.get("1.0", "end-1c")
            urls = [line.strip() for line in raw.splitlines()
                    if line.strip() and not line.strip().startswith("#")]
            if not urls:
                self._log("Nothing to add: paste one URL per line first.")
                return
            self._queue_urls(urls)
            self.url_text.delete("1.0", "end")

        def _queue_urls(self, urls: List[str]) -> None:
            self._apply_settings()
            accepted, rejected = 0, []
            for url in urls:
                try:
                    self.manager.add(url, connections=self.manager.connections)
                    accepted += 1
                except DownloadError as exc:
                    rejected.append(str(exc))
            if accepted:
                self._log("Queued %d URL(s) in %s" % (accepted, self.manager.outdir))
            for problem in rejected:
                self._log("Skipped: %s" % problem)
            if accepted:
                self._refresh()

        def _start(self) -> None:
            self._apply_settings()
            if self.url_text.get("1.0", "end-1c").strip():
                self._add_urls()
            if not self.manager.tasks:
                messagebox.showinfo("smalldownloader", "Add at least one URL first.")
                return
            try:
                os.makedirs(self.manager.outdir, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(
                    "smalldownloader", "Cannot create the download folder:\n%s" % exc)
                return
            self.manager.start()
            self._log("Starting: %d connection(s) per file, %d file(s) at a time." % (
                self.manager.connections, self.manager.jobs))
            self._refresh()

        def _pause(self) -> None:
            self.manager.pause_all()
            self._log("Pausing — partial files are kept and can be resumed.")
            self._refresh()

        def _resume(self) -> None:
            self._apply_settings()
            self.manager.resume_all()
            self._log("Resuming…")
            self._refresh()

        def _cancel(self) -> None:
            if messagebox.askyesno(
                "smalldownloader",
                "Cancel everything?\n\nPartial files are kept, so a retry later resumes them.",
            ):
                self.manager.cancel_all()
                self._log("Cancelling…")
                self._refresh()

        def _retry_failed(self) -> None:
            failed = [task for task in self.manager.tasks if task.state in (ERROR, CANCELLED)]
            if not failed:
                self._log("No failed downloads to retry.")
                return
            for task in failed:
                task.reset()
            self.manager.start()
            self._log("Retrying %d download(s)…" % len(failed))
            self._refresh()

        def _clear_finished(self) -> None:
            self.manager.clear_finished()
            self._log("Removed finished downloads from the list.")
            self._refresh()

        # -- per-selection actions ----------------------------------------- #

        def _pause_selected(self) -> None:
            selected = self._selected_tasks()
            for task in selected:
                if task.is_active:
                    task.pause()
            self._log("Paused %d selected download(s)." % len(selected))
            self._refresh()

        def _resume_selected(self) -> None:
            selected = self._selected_tasks()
            for task in selected:
                if task.state in (PAUSED, CANCELLED, ERROR):
                    task.reset()
            if selected:
                self.manager.start()
                self._log("Resuming %d selected download(s)…" % len(selected))
            self._refresh()

        def _cancel_selected(self) -> None:
            selected = self._selected_tasks()
            for task in selected:
                task.cancel()
            self._log("Cancelled %d selected download(s)." % len(selected))
            self._refresh()

        def _retry_selected(self) -> None:
            selected = self._selected_tasks()
            for task in selected:
                if task.state in (PAUSED, CANCELLED, ERROR, DONE):
                    task.reset()
            if selected:
                self.manager.start()
            self._refresh()

        def _remove_selected(self) -> None:
            for task in self._selected_tasks():
                self.manager.remove(task)
                row = self._rows.pop(task.id, None)
                if row:
                    self.tree.delete(row)
                self._last_state.pop(task.id, None)
            self._refresh()

        def _open_selected(self) -> None:
            for task in self._selected_tasks():
                if task.final_path:
                    _open_path(task.final_path)
                    return

        def _reveal_selected(self) -> None:
            for task in self._selected_tasks():
                if task.final_path:
                    _open_path(task.final_path, reveal=True)
                    return
                _open_path(self.manager.outdir)

        def _copy_selected_url(self) -> None:
            tasks = self._selected_tasks()
            if not tasks:
                return
            text = "\n".join(task.url for task in tasks)
            self.clipboard_clear()
            self.clipboard_append(text)
            self._log("Copied %d URL(s) to the clipboard." % len(tasks))

        def _on_double_click(self, event) -> None:
            row = self.tree.identify_row(event.y)
            if not row:
                return
            task = self._task(row)
            if task and task.final_path:
                _open_path(task.final_path)

        def _on_right_click(self, event) -> None:
            row = self.tree.identify_row(event.y)
            if row:
                if row not in self.tree.selection():
                    self.tree.selection_set(row)
                self.tree.focus(row)
                try:
                    self.context_menu.tk_popup(event.x_root, event.y_root)
                finally:
                    self.context_menu.grab_release()

        # -- cURL import ---------------------------------------------------- #

        def _open_curl_dialog(self) -> None:
            c = self.colors
            dialog = tk.Toplevel(self)
            dialog.title("Import from browser cURL")
            dialog.geometry("760x480")
            dialog.minsize(560, 390)
            dialog.configure(bg=c["bg"])
            dialog.transient(self)
            dialog.grab_set()

            shell = ttk.Frame(dialog, style="App.TFrame", padding=16)
            shell.pack(fill="both", expand=True)
            ttk.Label(shell, text="Import a browser request", style="Head.TLabel").pack(
                anchor="w")
            ttk.Label(shell, text="Paste “Copy as cURL” from your browser’s DevTools.",
                      style="Subhead.TLabel").pack(anchor="w", pady=(2, 10))
            text = tk.Text(
                shell, bg=c["field"], fg=c["text"], insertbackground=c["text"],
                selectbackground=c["selection"], selectforeground=c["text"], relief="flat",
                highlightthickness=1, highlightbackground=c["border"],
                highlightcolor=c["accent"], wrap="word", font=(self._mono_font(), 9),
                padx=10, pady=8, cursor="xterm",
            )
            text.pack(fill="both", expand=True, pady=(0, 9))
            ttk.Label(
                shell,
                text=("Headers, cookies, and POST bodies are applied to this session "
                      "and its queued links."),
                style="Muted.TLabel",
            ).pack(anchor="w")

            buttons = ttk.Frame(shell, style="App.TFrame")
            buttons.pack(fill="x", pady=(12, 0))

            def apply() -> None:
                parsed = parse_curl(text.get("1.0", "end-1c"))
                if not parsed.url:
                    messagebox.showwarning("smalldownloader", "No URL found in that snippet.")
                    return
                self.manager.headers.update(parsed.headers)
                self.manager.method = parsed.method
                self.manager.post_data = parsed.as_post_data()
                for task in self.manager.tasks:
                    if task.state in (PENDING, PAUSED):
                        task.headers.update(parsed.headers)
                        task.method = self.manager.method
                        task.post_data = self.manager.post_data
                self.url_text.delete("1.0", "end")
                self.url_text.insert("1.0", parsed.url + "\n")
                self.url_text.focus_set()
                self._log("cURL imported: %d header(s), method %s." % (
                    len(parsed.headers), parsed.method or "GET"))
                dialog.destroy()

            self._button(buttons, "Cancel", command=dialog.destroy,
                         style="Quiet.TButton").pack(side="right")
            self._button(buttons, "Use this request", command=apply,
                         style="Primary.TButton").pack(side="right", padx=(0, 6))
            text.focus_set()

        # -- about ---------------------------------------------------------- #

        def _about(self) -> None:
            messagebox.showinfo(
                "About smalldownloader",
                "smalldownloader %s\n\n"
                "A tiny, dependency-free downloader.\n"
                "Multi-connection, resumable, terminal + GUI.\n"
                "The desktop UI includes light and dark appearances.\n\n"
                "Partial files are stored as '<name>.smlpart' plus a '<name>.smlpart.json'\n"
                "progress file; rerunning the same URL resumes automatically." % __version__,
            )

        # ------------------------------------------------------------------ #
        # polling / rendering
        # ------------------------------------------------------------------ #

        def _poll(self) -> None:
            if self._closing:
                return
            try:
                self._refresh()
            except tk.TclError:  # window went away underneath us
                return
            self.after(POLL_MS, self._poll)

        def _refresh(self) -> None:
            states = self.manager.progress()
            self._progress_by_id = {state.id: state for state in states}
            seen = set()
            for index, state in enumerate(states, 1):
                seen.add(state.id)
                row = self._rows.get(state.id)
                if row is None:
                    row = self.tree.insert("", "end", iid=str(state.id),
                                           values=self._row_values(index, state))
                    self._rows[state.id] = row
                else:
                    self.tree.item(row, values=self._row_values(index, state))
                self.tree.item(row, tags=(state.state,))
                self._log_transition(state)
            for task_id in list(self._rows):
                if task_id not in seen:
                    self.tree.delete(self._rows.pop(task_id))
                    self._last_state.pop(task_id, None)
            self._show_empty_queue(not states)
            self._sort_rows()

            counts = self.manager.counts()
            speed = self.manager.total_speed
            self.queue_count_var.set("%d download%s" % (
                len(states), "" if len(states) == 1 else "s"))
            self.stat_vars["active"].set(str(counts[RUNNING]))
            self.stat_vars["queued"].set(str(counts[PENDING] + counts[PAUSED]))
            self.stat_vars["completed"].set(str(counts[DONE]))
            self.stat_vars["speed"].set("%s/s" % human_bytes(speed) if speed > 1 else "0 B/s")

            if counts[RUNNING]:
                status, status_style = "●  Downloading", "ActiveStatus.TLabel"
            elif counts[ERROR]:
                status, status_style = "●  Attention needed", "ErrorStatus.TLabel"
            elif counts[PAUSED]:
                status, status_style = "●  Paused", "WarningStatus.TLabel"
            elif counts[PENDING]:
                status, status_style = "●  Ready to start", "ActiveStatus.TLabel"
            else:
                status, status_style = "●  Ready", "Ready.TLabel"
            self.status_label.configure(text=status, style=status_style)

            active = counts[RUNNING] + counts[PENDING]
            retryable = counts[ERROR] + counts[CANCELLED]
            finished = counts[DONE] + retryable
            self.pause_button.configure(state="normal" if active else "disabled")
            self.resume_button.configure(
                state="normal" if counts[PAUSED] or counts[PENDING] else "disabled")
            self.cancel_button.configure(state="normal" if active else "disabled")
            self.retry_button.configure(state="normal" if retryable else "disabled")
            self.clear_button.configure(state="normal" if finished else "disabled")
            self._update_details()

        def _row_values(self, index: int, state: Progress) -> tuple:
            if state.total > 0:
                fraction = state.downloaded / float(state.total)
                percent = "%3.0f%%" % state.percent
                amount = "%s / %s" % (human_bytes(state.downloaded), human_bytes(state.total))
            else:
                fraction = 0.0
                percent = " --"
                amount = human_bytes(state.downloaded)
            if state.state == DONE:
                fraction = 1.0
                percent = "100%"
            width = 10
            filled = int(round(max(0.0, min(1.0, fraction)) * width))
            bar = "■" * filled + "·" * (width - filled)
            if state.state == ERROR:
                bar = "—" * width
            speed = ("%s/s" % human_bytes(state.speed)) if state.speed > 1 else "-"
            eta = human_time(state.eta) if state.eta is not None else "-"
            label = STATE_LABELS.get(state.state, state.state)
            if state.state == ERROR and state.error:
                detail = " ".join(str(state.error).split())
                label = "failed · %s" % (detail[:34] + ("…" if len(detail) > 34 else ""))
            return (index, state.name, "%s %s" % (bar, percent), amount, speed, eta, label)

        def _log_transition(self, state: Progress) -> None:
            previous = self._last_state.get(state.id)
            if previous == state.state:
                return
            self._last_state[state.id] = state.state
            if previous is None and state.state == PENDING:
                return
            if state.state == RUNNING:
                self._log("↓ %s" % state.name)
            elif state.state == DONE:
                self._log("✔ %s  →  %s" % (state.name, state.dest or self.manager.outdir))
            elif state.state == ERROR:
                self._log("✘ %s: %s" % (state.name, state.error or "unknown error"))
            elif state.state == PAUSED:
                self._log("⏸ %s paused at %s of %s" % (
                    state.name,
                    human_bytes(state.downloaded),
                    human_bytes(state.total) if state.total > 0 else "?",
                ))
            elif state.state == CANCELLED:
                self._log("⏹ %s cancelled (%s kept for resume)" % (state.name,
                                                                    human_bytes(state.downloaded)))

        def _update_details(self) -> None:
            self._update_details_for(self._selected_tasks())

        def _update_details_for(self, tasks: List[object]) -> None:
            if len(tasks) != 1:
                if len(tasks) > 1:
                    self.detail_var.set(
                        "%d downloads selected. Select one item to inspect its details."
                        % len(tasks))
                else:
                    self.detail_var.set(
                        "Select a download to see its URL, progress, and save location.")
                return
            task = tasks[0]
            state = task.progress()
            lines = [
                "File      : %s" % state.name,
                "URL       : %s" % state.url,
                "Status    : %s%s" % (
                    STATE_LABELS.get(state.state, state.state),
                    "  (%s)" % state.error if state.error else "",
                ),
                "Progress  : %s of %s%s" % (
                    human_bytes(state.downloaded),
                    human_bytes(state.total) if state.total > 0 else "unknown",
                    "  (%.1f%%)" % state.percent if state.total > 0 else "",
                ),
                "Speed     : %s/s" % human_bytes(state.speed),
                "Saving to : %s" % (state.dest or os.path.join(self.manager.outdir, state.name)),
                "Mode      : %s, %d connection(s)" % (
                    "resumable" if task.resume else "no resume", task.connections),
            ]
            if task.segments and len(task.segments) > 1:
                lines.append("Segments  : %d" % len(task.segments))
            if task.headers:
                lines.append("Headers   : %s" % ", ".join(sorted(task.headers)))
            self.detail_var.set("\n".join(lines))

        # ------------------------------------------------------------------ #

        def _on_close(self) -> None:
            active = [task for task in self.manager.tasks if task.is_active]
            if active:
                keep = messagebox.askyesno(
                    "smalldownloader",
                    "%d download(s) are active or waiting.\n\n"
                    "Quit anyway? Partial files are kept, so you can resume later." % len(active),
                )
                if not keep:
                    return
            self._closing = True
            try:
                self.manager.stop(pause=True, wait=False)
            except Exception:
                pass
            self.destroy()

    _APP_CLASS = DownloaderApp
    return _APP_CLASS


def __getattr__(name: str):
    """Keep ``gui.DownloaderApp`` working without importing Tk at import time."""
    if name == "DownloaderApp":
        return app_class()
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def run_gui(urls: Optional[List[str]] = None, outdir: Optional[str] = None) -> int:
    """Launch the window. Returns a process exit code (1 when Tk is unavailable)."""
    try:
        window_class = app_class()
    except ImportError as exc:  # tkinter is not installed
        print("smalldownloader: the GUI needs Tkinter (%s)." % exc, file=sys.stderr)
        print("  Debian/Ubuntu : sudo apt install python3-tk", file=sys.stderr)
        print("  Fedora        : sudo dnf install python3-tkinter", file=sys.stderr)
        print("  macOS/Windows : use the python.org build of Python", file=sys.stderr)
        print("Use the terminal mode meanwhile, e.g. smalldownloader <URL>.", file=sys.stderr)
        return 1
    try:
        app = window_class(urls=urls, outdir=outdir)
    except tk.TclError as exc:  # pragma: no cover - no display
        print("smalldownloader: cannot open a window here (%s)." % exc, file=sys.stderr)
        print("Use the terminal mode instead, e.g. smalldownloader <URL>.", file=sys.stderr)
        return 1
    try:
        app.mainloop()
    finally:
        app.manager.stop(pause=True, wait=False)
    return 0
