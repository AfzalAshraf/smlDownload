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
            self.geometry("1080x720")
            self.minsize(880, 560)
            self.configure(bg=BG)
            self.protocol("WM_DELETE_WINDOW", self._on_close)

            self.manager = DownloadManager(
                outdir=outdir or default_download_dir(),
                connections=4,
                jobs=2,
            )
            self._rows: Dict[int, str] = {}
            self._last_state: Dict[int, str] = {}
            self._closing = False

            self._build_style()
            self._build_menu()
            self._build_widgets()

            for url in urls or []:
                self.url_text.insert("end", url + "\n")
            self._poll()
            self._log("smalldownloader %s - ready. Paste links, then press Start." % __version__)

        # ------------------------------------------------------------------ #
        # construction
        # ------------------------------------------------------------------ #

        def _build_style(self) -> None:
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL_2,
                            bordercolor=BORDER, focuscolor=ACCENT, darkcolor=PANEL,
                            lightcolor=PANEL, troughcolor=PANEL_2)
            style.configure("TFrame", background=BG)
            style.configure("Panel.TFrame", background=PANEL)
            style.configure("TLabel", background=BG, foreground=FG)
            style.configure("Muted.TLabel", background=BG, foreground=MUTED)
            style.configure("Panel.TLabel", background=PANEL, foreground=FG)
            style.configure("Head.TLabel", background=BG, foreground=FG,
                            font=(self._ui_font(), 15, "bold"))
            style.configure("TButton", background=PANEL_2, foreground=FG, borderwidth=1,
                            focusthickness=0, padding=(10, 6))
            style.map("TButton",
                      background=[("active", BORDER), ("disabled", PANEL)],
                      foreground=[("disabled", MUTED)])
            style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
            style.map("Accent.TButton",
                      background=[("active", ACCENT_HOVER), ("disabled", "#2b3a52")],
                      foreground=[("disabled", "#9aa6bd")])
            style.configure("TEntry", fieldbackground=PANEL_2, foreground=FG,
                            insertcolor=FG, bordercolor=BORDER, padding=6)
            style.configure("TSpinbox", fieldbackground=PANEL_2, foreground=FG,
                            arrowcolor=FG, bordercolor=BORDER, padding=4)
            style.configure("TCheckbutton", background=BG, foreground=FG,
                            focuscolor=BG, indicatorcolor=PANEL_2)
            style.map("TCheckbutton", background=[("active", BG)])
            style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                            rowheight=26, borderwidth=0, font=(self._mono_font(), 10))
            style.configure("Treeview.Heading", background=PANEL_2, foreground=MUTED,
                            relief="flat", font=(self._ui_font(), 10, "bold"))
            style.map("Treeview.Heading", background=[("active", BORDER)])
            style.map("Treeview", background=[("selected", "#2b3a52")],
                      foreground=[("selected", "#ffffff")])
            style.configure("TNotebook", background=BG, borderwidth=0)
            style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(14, 7))
            style.map("TNotebook.Tab", background=[("selected", PANEL_2)],
                      foreground=[("selected", FG)])
            style.configure("TProgressbar", background=ACCENT, troughcolor=PANEL_2, borderwidth=0)

        def _ui_font(self) -> str:
            return self._pick_font(("Segoe UI", "Helvetica Neue", "DejaVu Sans", "Arial"))

        def _mono_font(self) -> str:
            return self._pick_font(("Consolas", "DejaVu Sans Mono", "Menlo", "Courier New",
                                   "Courier"))

        def _pick_font(self, candidates) -> str:
            try:
                available = {name.lower() for name in tkfont.families(self)}
            except tk.TclError:
                return "TkDefaultFont"
            for name in candidates:
                if name.lower() in available:
                    return name
            return "TkDefaultFont"

        def _build_menu(self) -> None:
            menubar = tk.Menu(self, tearoff=0)
            file_menu = tk.Menu(menubar, tearoff=0)
            file_menu.add_command(label="Add URLs from file…", command=self._add_from_file)
            file_menu.add_command(label="Import from browser cURL…", command=self._open_curl_dialog)
            file_menu.add_separator()
            file_menu.add_command(label="Choose download folder…", command=self._browse_dir)
            file_menu.add_separator()
            file_menu.add_command(label="Quit", command=self._on_close)
            menubar.add_cascade(label="File", menu=file_menu)

            queue_menu = tk.Menu(menubar, tearoff=0)
            queue_menu.add_command(label="Start", command=self._start)
            queue_menu.add_command(label="Pause all", command=self._pause)
            queue_menu.add_command(label="Resume all", command=self._resume)
            queue_menu.add_command(label="Cancel all", command=self._cancel)
            queue_menu.add_separator()
            queue_menu.add_command(label="Retry failed", command=self._retry_failed)
            queue_menu.add_command(label="Remove finished", command=self._clear_finished)
            menubar.add_cascade(label="Queue", menu=queue_menu)

            help_menu = tk.Menu(menubar, tearoff=0)
            help_menu.add_command(label="About", command=self._about)
            menubar.add_cascade(label="Help", menu=help_menu)
            self.configure(menu=menubar)

        def _build_widgets(self) -> None:
            self.grid_rowconfigure(2, weight=3)
            self.grid_rowconfigure(3, weight=2)
            self.grid_columnconfigure(0, weight=1)

            # -- header ---------------------------------------------------- #
            header = ttk.Frame(self, style="TFrame")
            header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
            header.grid_columnconfigure(1, weight=1)
            ttk.Label(header, text="smalldownloader", style="Head.TLabel").grid(
                row=0, column=0, sticky="w")
            self.status_label = ttk.Label(header, text="idle", style="Muted.TLabel")
            self.status_label.grid(row=0, column=1, sticky="e")

            # -- settings bar ---------------------------------------------- #
            settings = ttk.Frame(self, style="Panel.TFrame")
            settings.grid(row=1, column=0, sticky="ew", padx=16, pady=6)
            settings.grid_columnconfigure(1, weight=1)

            ttk.Label(settings, text="Save to", style="Panel.TLabel").grid(
                row=0, column=0, padx=(12, 8), pady=10)
            self.dir_var = tk.StringVar(value=self.manager.outdir)
            dir_entry = ttk.Entry(settings, textvariable=self.dir_var)
            dir_entry.grid(row=0, column=1, sticky="ew", pady=10)
            ttk.Button(settings, text="Browse…", command=self._browse_dir).grid(
                row=0, column=2, padx=8, pady=10)

            ttk.Label(settings, text="Connections", style="Panel.TLabel").grid(
                row=1, column=0, padx=(12, 8), pady=(0, 10), sticky="w")
            self.conn_var = tk.StringVar(value="4")
            ttk.Spinbox(settings, from_=1, to=32, width=5, textvariable=self.conn_var).grid(
                row=1, column=1, sticky="w", pady=(0, 10))

            right = ttk.Frame(settings, style="Panel.TFrame")
            right.grid(row=1, column=2, sticky="e", padx=8, pady=(0, 10))
            ttk.Label(right, text="Files at once", style="Panel.TLabel").pack(side="left", padx=(0, 6))
            self.jobs_var = tk.StringVar(value="2")
            ttk.Spinbox(right, from_=1, to=16, width=5, textvariable=self.jobs_var).pack(side="left")

            # -- URL box --------------------------------------------------- #
            body = ttk.Frame(self, style="TFrame")
            body.grid(row=2, column=0, sticky="nsew", padx=16, pady=6)
            body.grid_rowconfigure(2, weight=1)
            body.grid_columnconfigure(0, weight=1)

            input_row = ttk.Frame(body, style="TFrame")
            input_row.grid(row=0, column=0, sticky="ew", pady=(0, 6))
            input_row.grid_columnconfigure(0, weight=1)
            self.url_text = tk.Text(input_row, height=3, bg=PANEL_2, fg=FG, insertbackground=FG,
                                    relief="flat", highlightthickness=1, highlightbackground=BORDER,
                                    highlightcolor=ACCENT, wrap="none", font=(self._mono_font(), 10),
                                    padx=8, pady=6)
            self.url_text.grid(row=0, column=0, sticky="ew")
            url_scroll = ttk.Scrollbar(input_row, orient="vertical", command=self.url_text.yview)
            url_scroll.grid(row=0, column=1, sticky="ns")
            self.url_text.configure(yscrollcommand=url_scroll.set)

            button_row = ttk.Frame(body, style="TFrame")
            button_row.grid(row=1, column=0, sticky="ew", pady=(4, 8))
            ttk.Button(button_row, text="Add to queue", command=self._add_urls).pack(side="left")
            ttk.Button(button_row, text="Import cURL…", command=self._open_curl_dialog).pack(
                side="left", padx=6)
            ttk.Button(button_row, text="Start", style="Accent.TButton",
                       command=self._start).pack(side="left", padx=(18, 6))
            ttk.Button(button_row, text="Pause", command=self._pause).pack(side="left", padx=6)
            ttk.Button(button_row, text="Resume", command=self._resume).pack(side="left", padx=6)
            ttk.Button(button_row, text="Cancel", command=self._cancel).pack(side="left", padx=6)
            ttk.Button(button_row, text="Retry failed", command=self._retry_failed).pack(
                side="left", padx=(18, 6))
            ttk.Button(button_row, text="Remove finished", command=self._clear_finished).pack(
                side="left", padx=6)

            # -- table ----------------------------------------------------- #
            table_wrap = ttk.Frame(body, style="TFrame")
            table_wrap.grid(row=2, column=0, sticky="nsew")
            table_wrap.grid_rowconfigure(0, weight=1)
            table_wrap.grid_columnconfigure(0, weight=1)

            columns = ("num", "name", "progress", "done", "speed", "eta", "status")
            self.tree = ttk.Treeview(table_wrap, columns=columns, show="headings",
                                     selectmode="extended")
            headings = {
                "num": "#", "name": "File", "progress": "Progress", "done": "Size",
                "speed": "Speed", "eta": "ETA", "status": "Status",
            }
            widths = {"num": 42, "name": 300, "progress": 190, "done": 150, "speed": 100,
                      "eta": 74, "status": 220}
            anchors = {"num": "center", "progress": "w", "done": "e", "speed": "e", "eta": "e"}
            for column in columns:
                self.tree.heading(column, text=headings[column])
                self.tree.column(column, width=widths[column], anchor=anchors.get(column, "w"),
                                 stretch=column in ("name", "status"))
            self.tree.grid(row=0, column=0, sticky="nsew")
            tree_scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
            tree_scroll.grid(row=0, column=1, sticky="ns")
            self.tree.configure(yscrollcommand=tree_scroll.set)
            for state, color in STATE_COLORS.items():
                self.tree.tag_configure(state, foreground=color)

            self.tree.bind("<Double-1>", self._on_double_click)
            self.tree.bind("<Button-3>", self._on_right_click)
            self.tree.bind("<Button-2>", self._on_right_click)
            self.tree.bind("<Delete>", lambda _event: self._remove_selected())

            # -- log -------------------------------------------------------- #
            log_wrap = ttk.Frame(self, style="TFrame")
            log_wrap.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 6))
            log_wrap.grid_rowconfigure(0, weight=1)
            log_wrap.grid_columnconfigure(0, weight=1)
            self.notebook = ttk.Notebook(log_wrap)
            self.notebook.grid(row=0, column=0, sticky="nsew")

            log_tab = ttk.Frame(self.notebook, style="TFrame")
            log_tab.grid_rowconfigure(0, weight=1)
            log_tab.grid_columnconfigure(0, weight=1)
            self.log_text = tk.Text(log_tab, height=7, bg=PANEL, fg=FG, relief="flat",
                                    highlightthickness=0, wrap="word",
                                    font=(self._mono_font(), 9), padx=8, pady=6)
            self.log_text.grid(row=0, column=0, sticky="nsew")
            log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log_text.yview)
            log_scroll.grid(row=0, column=1, sticky="ns")
            self.log_text.configure(yscrollcommand=log_scroll.set, state="disabled")
            self.notebook.add(log_tab, text="Activity")

            info_tab = ttk.Frame(self.notebook, style="TFrame")
            info_tab.grid_rowconfigure(1, weight=1)
            info_tab.grid_columnconfigure(0, weight=1)
            self.detail_var = tk.StringVar(value="Select a download to see its details.")
            ttk.Label(info_tab, textvariable=self.detail_var, justify="left",
                      font=(self._mono_font(), 10)).grid(row=0, column=0, sticky="nw",
                                                         padx=12, pady=10)
            self.notebook.add(info_tab, text="Details")
            self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_details())

            self._build_context_menu()

        def _build_context_menu(self) -> None:
            self.context_menu = tk.Menu(self, tearoff=0)
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
                self.manager.connections = max(1, int(float(self.conn_var.get())))
            except ValueError:
                self.manager.connections = 4
            try:
                self.manager.jobs = max(1, int(float(self.jobs_var.get())))
            except ValueError:
                self.manager.jobs = 2
            self.dir_var.set(self.manager.outdir)

        # ------------------------------------------------------------------ #
        # actions
        # ------------------------------------------------------------------ #

        def _browse_dir(self) -> None:
            chosen = filedialog.askdirectory(initialdir=self.dir_var.get() or os.path.expanduser("~"))
            if chosen:
                self.dir_var.set(chosen)

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

        def _start(self) -> None:
            self._apply_settings()
            if self.url_text.get("1.0", "end-1c").strip():
                self._add_urls()
            if not self.manager.tasks:
                messagebox.showinfo("smalldownloader", "Add at least one URL first.")
                return
            os.makedirs(self.manager.outdir, exist_ok=True)
            self.manager.start()
            self._log("Starting: %d connection(s) per file, %d file(s) at a time." % (
                self.manager.connections, self.manager.jobs))

        def _pause(self) -> None:
            self.manager.pause_all()
            self._log("Pausing - partial files are kept and can be resumed.")

        def _resume(self) -> None:
            self._apply_settings()
            self.manager.resume_all()
            self._log("Resuming…")

        def _cancel(self) -> None:
            if messagebox.askyesno(
                "smalldownloader",
                "Cancel everything?\n\nPartial files are kept, so a retry later resumes them.",
            ):
                self.manager.cancel_all()
                self._log("Cancelling…")

        def _retry_failed(self) -> None:
            failed = [task for task in self.manager.tasks if task.state in (ERROR, CANCELLED)]
            if not failed:
                self._log("No failed downloads to retry.")
                return
            for task in failed:
                task.reset()
            self.manager.start()
            self._log("Retrying %d download(s)…" % len(failed))

        def _clear_finished(self) -> None:
            self.manager.clear_finished()
            for task_id in list(self._rows):
                if self._task(task_id) is None:
                    self.tree.delete(self._rows.pop(task_id))
                    self._last_state.pop(task_id, None)
            self._log("Removed finished downloads from the list.")

        # -- per-selection actions ----------------------------------------- #

        def _pause_selected(self) -> None:
            for task in self._selected_tasks():
                if task.is_active:
                    task.pause()
            self._log("Paused %d selected download(s)." % len(self.tree.selection()))

        def _resume_selected(self) -> None:
            for task in self._selected_tasks():
                if task.state in (PAUSED, CANCELLED, ERROR):
                    task.reset()
            self.manager.start()

        def _cancel_selected(self) -> None:
            for task in self._selected_tasks():
                task.cancel()
            self._log("Cancelled %d selected download(s)." % len(self.tree.selection()))

        def _retry_selected(self) -> None:
            for task in self._selected_tasks():
                task.reset()
            self.manager.start()

        def _remove_selected(self) -> None:
            for task in self._selected_tasks():
                self.manager.remove(task)
                row = self._rows.pop(task.id, None)
                if row:
                    self.tree.delete(row)
                self._last_state.pop(task.id, None)

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
            dialog = tk.Toplevel(self)
            dialog.title("Import from browser cURL")
            dialog.geometry("720x460")
            dialog.configure(bg=BG)
            dialog.transient(self)
            dialog.grab_set()

            ttk.Label(dialog, text="Paste 'Copy as cURL' from your browser's DevTools:",
                      style="TLabel").pack(anchor="w", padx=14, pady=(12, 6))
            text = tk.Text(dialog, bg=PANEL_2, fg=FG, insertbackground=FG, relief="flat",
                           highlightthickness=1, highlightbackground=BORDER, wrap="word",
                           font=(self._mono_font(), 9), padx=8, pady=6)
            text.pack(fill="both", expand=True, padx=14, pady=(0, 10))
            hint = ttk.Label(dialog, text="Headers, cookies and POST bodies are reused as-is.",
                             style="Muted.TLabel")
            hint.pack(anchor="w", padx=14)

            buttons = ttk.Frame(dialog, style="TFrame")
            buttons.pack(fill="x", padx=14, pady=12)

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
                self._log("cURL imported: %d header(s), method %s." % (
                    len(parsed.headers), parsed.method or "GET"))
                dialog.destroy()

            ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
            ttk.Button(buttons, text="Use this session", style="Accent.TButton",
                       command=apply).pack(side="right", padx=8)

        # -- about ---------------------------------------------------------- #

        def _about(self) -> None:
            messagebox.showinfo(
                "About smalldownloader",
                "smalldownloader %s\n\n"
                "A tiny, dependency-free downloader.\n"
                "Multi-connection, resumable, terminal + GUI.\n\n"
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

            counts = self.manager.counts()
            speed = self.manager.total_speed
            parts = []
            if counts[RUNNING]:
                parts.append("%d running" % counts[RUNNING])
            if counts[PENDING]:
                parts.append("%d queued" % counts[PENDING])
            if counts[PAUSED]:
                parts.append("%d paused" % counts[PAUSED])
            if counts[DONE]:
                parts.append("%d done" % counts[DONE])
            if counts[ERROR]:
                parts.append("%d failed" % counts[ERROR])
            if counts[CANCELLED]:
                parts.append("%d cancelled" % counts[CANCELLED])
            if speed > 1:
                parts.append("%s/s" % human_bytes(speed))
            self.status_label.configure(text="  ·  ".join(parts) if parts else "idle")
            self._update_details()

        def _row_values(self, index: int, state: Progress) -> tuple:
            if state.total > 0:
                fraction = state.downloaded / float(state.total)
                percent = "%3.0f%%" % state.percent
                amount = "%s / %s" % (human_bytes(state.downloaded), human_bytes(state.total))
            else:
                fraction = 0.0
                percent = "  ? "
                amount = human_bytes(state.downloaded)
            if state.state == DONE:
                fraction = 1.0
            width = 22
            filled = int(round(max(0.0, min(1.0, fraction)) * width))
            bar = "█" * filled + "░" * (width - filled)
            if state.state == ERROR:
                bar = "—" * width
            speed = ("%s/s" % human_bytes(state.speed)) if state.speed > 1 else "-"
            eta = human_time(state.eta) if state.eta else "-"
            label = STATE_LABELS.get(state.state, state.state)
            if state.state == ERROR and state.error:
                label = "error: %s" % state.error
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
                    self.detail_var.set("%d downloads selected." % len(tasks))
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
                    "%d download(s) are still running.\n\n"
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
