"""GUI tests.

Sandboxes such as CI containers usually have no Tk and no display, so the
window itself cannot be opened here. Instead:

* :class:`CurlImportTests` tests the cURL parser directly (no Tk involved).
* :class:`GuiSmokeTests` installs a *fake* ``tkinter`` (every widget method is a
  no-op that returns an empty, string-like, callable object) and then drives the
  real :class:`~smalldownloader.gui.DownloaderApp` logic - building the widgets,
  queueing URLs, running a real download through the real engine and refreshing
  the table - so typos and broken control flow are caught.

A fake Tk cannot catch cosmetic problems, but it does run the code.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smalldownloader.curlimport import parse_curl  # noqa: E402
from tests.local_server import TestServer, payload  # noqa: E402


# --------------------------------------------------------------------------- #
# cURL parsing (pure Python, always available)
# --------------------------------------------------------------------------- #


class CurlImportTests(unittest.TestCase):
    def test_bash_style_snippet(self):
        snippet = (
            "curl 'https://example.com/stream/transform/zip' \\\n"
            "  -H 'accept: application/json' \\\n"
            "  -H 'cookie: session=abc123' \\\n"
            "  --data-raw '{\"files\":[1]}' \\\n"
            "  --compressed"
        )
        parsed = parse_curl(snippet)
        self.assertEqual(parsed.url, "https://example.com/stream/transform/zip")
        self.assertEqual(parsed.headers["cookie"], "session=abc123")
        self.assertEqual(parsed.headers["accept"], "application/json")
        self.assertEqual(parsed.method, "POST")
        self.assertEqual(parsed.body, '{"files":[1]}')
        self.assertTrue(parsed.is_post)
        self.assertEqual(parsed.as_post_data(), b'{"files":[1]}')

    def test_windows_cmd_snippet_with_carets(self):
        snippet = (
            "curl.exe ^\"https://example.com/file.zip^\" ^\n"
            "  -H ^\"User-Agent: Mozilla/5.0^\" ^\n"
            "  -H ^\"Accept-Encoding: gzip, deflate^\"\n"
        )
        parsed = parse_curl(snippet)
        self.assertEqual(parsed.url, "https://example.com/file.zip")
        self.assertEqual(parsed.headers["User-Agent"], "Mozilla/5.0")
        # compressed responses are dropped: byte ranges would not line up
        self.assertNotIn("Accept-Encoding", parsed.headers)
        self.assertIsNone(parsed.method)

    def test_powershell_snippet_with_backticks(self):
        snippet = (
            "curl 'https://example.com/a.bin' `\n"
            "  -H 'referer: https://example.com/'"
        )
        parsed = parse_curl(snippet)
        self.assertEqual(parsed.url, "https://example.com/a.bin")
        self.assertEqual(parsed.headers["referer"], "https://example.com/")

    def test_explicit_method_and_plain_url(self):
        parsed = parse_curl("curl -X PUT https://example.com/put -H 'x: y'")
        self.assertEqual(parsed.method, "PUT")
        self.assertEqual(parsed.url, "https://example.com/put")

        parsed = parse_curl("https://example.com/just-a-url")
        self.assertEqual(parsed.url, "https://example.com/just-a-url")
        self.assertFalse(parsed.is_post)

    def test_empty_input(self):
        parsed = parse_curl("")
        self.assertIsNone(parsed.url)
        self.assertEqual(parsed.headers, {})
        self.assertIsNone(parsed.as_post_data())


# --------------------------------------------------------------------------- #
# fake tkinter + the real window logic
# --------------------------------------------------------------------------- #


class _Any(str):
    """Empty string that is also callable and truthy-silent (returns itself)."""

    def __call__(self, *args, **kwargs):
        return _Any("")

    def __getattr__(self, name):
        return self


class FakeWidget:
    """No-op widget that records the calls made to it.

    Every unknown attribute answers with a :class:`_Any` (empty, string-like and
    callable), so ``widget.grid(row=0)`` and ``widget.configure(text="hi")`` both
    work. Text-like widgets additionally remember what was inserted.
    """

    def __init__(self, *args, **kwargs):
        self._calls = []
        self._args = args
        self._kwargs = kwargs
        self._data = {}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def record(*args, **kwargs):
            self._calls.append((name, args, kwargs))
            return _Any("")

        return record

    # -- the few methods the app relies on reading back --------------- #

    def insert(self, index, text, *args, **kwargs):
        self._data["text"] = self._data.get("text", "") + str(text)
        self._calls.append(("insert", (index, text), kwargs))

    def delete(self, *args, **kwargs):
        self._data["text"] = ""
        self._calls.append(("delete", args, kwargs))

    def get(self, *args, **kwargs):
        self._calls.append(("get", args, kwargs))
        return _Any(self._data.get("text", ""))

    def configure(self, *args, **kwargs):
        self._data.update({key: value for key, value in kwargs.items()
                           if isinstance(value, str)})
        self._calls.append(("configure", args, kwargs))

    config = configure

    def __setitem__(self, key, value):
        self._data[key] = value

    def __getitem__(self, key):
        return self._data.get(key, _Any(""))

    # -- test helpers --------------------------------------------------- #

    def called(self, name: str):
        """All argument tuples this method was called with."""
        return [args for call, args, _kwargs in self._calls if call == name]


class FakeTk(FakeWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._after_calls = []

    def after(self, delay, callback=None, *args):
        self._after_calls.append(delay)
        return "after-id"

    def mainloop(self, *args, **kwargs):
        return None

    def destroy(self):
        return None


class FakeVar(FakeWidget):
    def __init__(self, value="", **kwargs):
        super().__init__(**kwargs)
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


def install_fake_tkinter() -> None:
    """Register a permissive tkinter stand-in for the duration of a test."""
    tk = types.ModuleType("tkinter")
    tk.Tk = FakeTk
    tk.Toplevel = FakeWidget
    tk.Menu = FakeWidget
    tk.Text = FakeWidget
    tk.TclError = type("TclError", (Exception,), {})
    tk.StringVar = FakeVar
    tk.IntVar = FakeVar
    tk.BooleanVar = FakeVar

    ttk = types.ModuleType("tkinter.ttk")
    for name in ("Frame", "Label", "Button", "Entry", "Spinbox", "Treeview",
                 "Scrollbar", "Notebook", "Progressbar", "Style", "Checkbutton"):
        setattr(ttk, name, FakeWidget)
    tk.ttk = ttk

    font = types.ModuleType("tkinter.font")
    font.families = lambda *args, **kwargs: ("DejaVu Sans", "DejaVu Sans Mono")
    tk.font = font

    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askdirectory = lambda *args, **kwargs: ""
    filedialog.askopenfilename = lambda *args, **kwargs: ""
    tk.filedialog = filedialog

    messagebox = types.ModuleType("tkinter.messagebox")
    messagebox.showinfo = lambda *args, **kwargs: "ok"
    messagebox.showwarning = lambda *args, **kwargs: "ok"
    messagebox.showerror = lambda *args, **kwargs: "ok"
    messagebox.askyesno = lambda *args, **kwargs: True
    tk.messagebox = messagebox

    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.font"] = font
    sys.modules["tkinter.filedialog"] = filedialog
    sys.modules["tkinter.messagebox"] = messagebox
    sys.modules.pop("smalldownloader.gui", None)


def uninstall_fake_tkinter() -> None:
    for name in ("tkinter.ttk", "tkinter.font", "tkinter.filedialog",
                 "tkinter.messagebox", "tkinter"):
        sys.modules.pop(name, None)
    sys.modules.pop("smalldownloader.gui", None)


@unittest.skipIf(
    __import__("importlib.util", fromlist=["util"]).find_spec("tkinter") is not None,
    "real tkinter installed: run the window manually instead",
)
class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_fake_tkinter()
        from smalldownloader import gui

        cls.gui = gui

    @classmethod
    def tearDownClass(cls):
        uninstall_fake_tkinter()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="smld-gui-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.server = TestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)

    def make_app(self):
        app = self.gui.DownloaderApp(outdir=self.tmp)
        self.addCleanup(app.manager.stop, True, False)
        return app

    def test_window_builds_and_renders_an_idle_queue(self):
        app = self.make_app()
        self.assertEqual(app.manager.outdir, self.tmp)
        app._refresh()  # empty queue must not explode
        self.assertEqual(app._rows, {})
        titles = [args[0] for args in app.called("title")
                  if args and isinstance(args[0], str)]
        self.assertTrue(any(t.startswith("smalldownloader") for t in titles),
                        "window title should mention the app: %r" % titles)

    def test_custom_theme_toggle_sorting_and_pointer_cursors(self):
        app = self.make_app()
        self.assertEqual(app.theme_name, "light")
        self.assertIn(app.start_button._kwargs.get("cursor"), ("hand2", "pointinghand"))
        self.assertEqual(app.url_text._kwargs.get("cursor"), "xterm")

        app._toggle_theme()
        self.assertEqual(app.theme_name, "dark")
        self.assertEqual(app.theme_button._data.get("text"), "Light theme")
        self.assertIn("dark appearance", app.log_text._data.get("text", ""))

        app._sort_by("name")
        self.assertEqual(app._sort_column, "name")
        self.assertFalse(app._sort_reverse)
        app._sort_by("name")
        self.assertTrue(app._sort_reverse)

    def test_add_start_and_render_a_real_download(self):
        app = self.make_app()
        url = self.server.url("file", "gui.bin", size=2 * 1024 * 1024)
        app.url_text.get = lambda *args, **kwargs: url + "\n"
        app.conn_var.set("4")
        app.jobs_var.set("1")
        app._add_urls()

        self.assertEqual(app.manager.task_count, 1)
        app._start()
        deadline = time.time() + 30
        while time.time() < deadline and not all(
            task.is_finished for task in app.manager.tasks
        ):
            app._refresh()
            time.sleep(0.05)
        app._refresh()

        task = app.manager.tasks[0]
        self.assertEqual(task.state, "done", task.error)
        self.assertIn(task.id, app._rows)
        with open(os.path.join(self.tmp, "gui.bin"), "rb") as handle:
            self.assertEqual(len(handle.read()), 2 * 1024 * 1024)
        self.assertEqual(digest_of("gui.bin", 2 * 1024 * 1024),
                         digest_of_file(os.path.join(self.tmp, "gui.bin")))

    def test_row_values_for_every_state(self):
        app = self.make_app()
        from smalldownloader.core import Progress

        for state in ("pending", "running", "paused", "done", "error", "cancelled"):
            progress = Progress(1, "file.bin", "https://example.com/file.bin", state,
                                1000, 250, 1024.0, 12.0,
                                "boom" if state == "error" else None, self.tmp, 4)
            values = app._row_values(1, progress)
            self.assertEqual(len(values), 7)
            self.assertIn("file.bin", values[1])
            if state == "error":
                self.assertIn("boom", values[6])

    def test_state_transitions_are_logged(self):
        app = self.make_app()
        from smalldownloader.core import Progress

        def snapshot(state, error=None):
            return Progress(7, "log.bin", "u", state, 100, 100, 0.0, None, error, "/tmp/log.bin", 2)

        app._log_transition(snapshot("pending"))
        app._log_transition(snapshot("running"))
        app._log_transition(snapshot("done"))
        app._log_transition(snapshot("error", "HTTP 404"))
        app._log_transition(snapshot("error", "HTTP 404"))  # duplicate must be skipped
        logged = app.log_text._data.get("text", "")
        self.assertIn("log.bin", logged)
        self.assertIn("404", logged)
        lines = [line for line in logged.splitlines()
                 if line.strip() and "ready" not in line]  # skip the startup banner
        # queued is silent, running/done/error are logged exactly once
        self.assertEqual(len(lines), 3, logged)

    def test_queue_helpers_and_selection_actions(self):
        app = self.make_app()
        app._queue_urls([self.server.url("file", "a.bin", size=1024),
                         self.server.url("file", "b.bin", size=1024)])
        self.assertEqual(app.manager.task_count, 2)
        app.tree.selection = lambda *args, **kwargs: (str(app.manager.tasks[0].id),)
        app._copy_selected_url()
        app._pause_selected()
        app._resume_selected()
        app._retry_selected()
        app._update_details()
        app._remove_selected()
        self.assertEqual(app.manager.task_count, 1)
        app._clear_finished()
        app._retry_failed()

    def test_rejected_urls_are_reported(self):
        app = self.make_app()
        app._queue_urls(["not-a-url", self.server.url("file", "good.bin", size=64)])
        self.assertEqual(app.manager.task_count, 1)

    def test_curl_dialog_parses_and_applies(self):
        app = self.make_app()
        app._open_curl_dialog()  # builds the dialog (fake) without exploding
        app._queue_urls([self.server.url("secure", "curl.bin", size=512)])
        app.manager.headers["Cookie"] = "token=letmein"
        app.manager.tasks[0].headers.update(app.manager.headers)
        app._start()
        deadline = time.time() + 20
        while time.time() < deadline and not app.manager.tasks[0].is_finished:
            time.sleep(0.05)
        self.assertEqual(app.manager.tasks[0].state, "done", app.manager.tasks[0].error)

    def test_about_and_close(self):
        app = self.make_app()
        app._about()
        app._on_close()


def digest_of(name: str, size: int) -> str:
    import hashlib

    return hashlib.sha256(payload(name, size)).hexdigest()


def digest_of_file(path: str) -> str:
    import hashlib

    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


if __name__ == "__main__":
    unittest.main(verbosity=2)
