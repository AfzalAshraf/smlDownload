"""Tests for the single-file edition (``smalldownloader.py``).

The bundle is generated from the package by ``tools/build_single_file.py``.
These tests make sure it stays in sync, that it really is standalone (no
``smalldownloader`` package, no ``PYTHONPATH``), and that it downloads the same
bytes as the package does.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.local_server import TestServer, payload  # noqa: E402

BUNDLE = os.path.join(ROOT, "smalldownloader.py")
BUILDER = os.path.join(ROOT, "tools", "build_single_file.py")


def isolated_env(extra=None):
    """A clean environment: no PYTHONPATH, no inherited Python fiddling."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra or {})
    return env


class SingleFileBuildTests(unittest.TestCase):
    def test_bundle_exists_and_compiles(self):
        self.assertTrue(os.path.exists(BUNDLE), "run tools/build_single_file.py")
        with open(BUNDLE, "r", encoding="utf-8") as handle:
            source = handle.read()
        compile(source, BUNDLE, "exec")
        self.assertIn("__version__", source)

    def test_bundle_is_up_to_date(self):
        """The committed single file must match the package (regenerate if not)."""
        result = subprocess.run(
            [sys.executable, BUILDER, "--check", "--quiet"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0,
                         "smalldownloader.py is stale:\n%s\nfix: python tools/build_single_file.py"
                         % (result.stdout + result.stderr))

    def test_bundle_has_no_package_imports(self):
        with open(BUNDLE, "r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                stripped = line.strip()
                if stripped.startswith(("from .", "from smalldownloader", "import smalldownloader")):
                    self.fail("line %d still imports the package: %s" % (number, stripped))

    def test_bundle_is_self_contained_without_pythonpath(self):
        workdir = tempfile.mkdtemp(prefix="smld-alone-")
        self.addCleanup(shutil.rmtree, workdir, True)
        copied = os.path.join(workdir, "smalldownloader.py")
        shutil.copy2(BUNDLE, copied)

        result = subprocess.run([sys.executable, copied, "--version"],
                                capture_output=True, text=True, cwd=workdir,
                                env=isolated_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("smalldownloader", result.stdout)

        # and the window command must fail with a friendly message, not a traceback
        result = subprocess.run([sys.executable, copied, "gui"],
                                capture_output=True, text=True, cwd=workdir,
                                env=isolated_env())
        if not __import__("importlib.util", fromlist=["util"]).find_spec("tkinter"):
            self.assertEqual(result.returncode, 1)
            self.assertIn("Tkinter", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


STUB_TKINTER = '''
"""Minimal stand-in for Tkinter: enough for smalldownloader's window to build."""
import types


class _Any(str):
    def __call__(self, *args, **kwargs):
        return _Any("")

    def __getattr__(self, name):
        return _Any("")


class FakeWidget:
    def __init__(self, *args, **kwargs):
        self._data = {}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any("")

    def insert(self, index, text, *args, **kwargs):
        self._data["text"] = self._data.get("text", "") + str(text)

    def delete(self, *args, **kwargs):
        self._data["text"] = ""

    def get(self, *args, **kwargs):
        return _Any(self._data.get("text", ""))

    def configure(self, *args, **kwargs):
        return None

    config = configure

    def __setitem__(self, key, value):
        self._data[key] = value

    def __getitem__(self, key):
        return self._data.get(key, _Any(""))


class Tk(FakeWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        print("STUB-TK-WINDOW-CREATED")

    def title(self, *args, **kwargs):
        return None

    def geometry(self, *args, **kwargs):
        return None

    def minsize(self, *args, **kwargs):
        return None

    def protocol(self, *args, **kwargs):
        return None

    def after(self, delay, callback=None, *args):
        return "after-id"

    def mainloop(self, *args, **kwargs):
        print("STUB-TK-MAINLOOP-REACHED")

    def destroy(self):
        return None


class StringVar(FakeWidget):
    def __init__(self, value="", **kwargs):
        super().__init__(**kwargs)
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


Toplevel = FakeWidget
Menu = FakeWidget
Text = FakeWidget
IntVar = StringVar
BooleanVar = StringVar
TclError = type("TclError", (Exception,), {})
'''


def write_stub_tkinter(directory: str) -> None:
    """Create a fake ``tkinter`` package so the GUI branch can run headless."""
    package = os.path.join(directory, "tkinter")
    os.makedirs(package, exist_ok=True)
    with open(os.path.join(package, "__init__.py"), "w", encoding="utf-8") as handle:
        handle.write(STUB_TKINTER)
    for submodule, names in {
        "ttk": ("Frame", "Label", "Button", "Entry", "Spinbox", "Treeview",
                "Scrollbar", "Notebook", "Progressbar", "Style", "Checkbutton"),
        "font": ("families",),
        "filedialog": ("askdirectory", "askopenfilename"),
        "messagebox": ("showinfo", "showwarning", "showerror", "askyesno"),
    }.items():
        with open(os.path.join(package, submodule + ".py"), "w", encoding="utf-8") as handle:
            handle.write("from tkinter import *  # noqa\n")
            handle.write("from tkinter import FakeWidget, _Any\n\n")
            for name in names:
                if name in ("families", "askdirectory", "askopenfilename", "showinfo",
                            "showwarning", "showerror", "askyesno"):
                    handle.write("%s = lambda *a, **k: _Any(\"\")\n" % name)
                else:
                    handle.write("%s = FakeWidget\n" % name)


class SingleFileDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="smld-1file-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.server = TestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)

    def run_bundle(self, argv, cwd=None):
        return subprocess.run([sys.executable, BUNDLE, *argv], capture_output=True,
                              text=True, cwd=cwd or os.path.dirname(BUNDLE),
                              env=isolated_env())

    def test_downloads_the_same_bytes_as_the_package(self):
        size = 3 * 1024 * 1024 + 12345
        url = self.server.url("file", "bundle.bin", size=size)
        result = self.run_bundle([url, "-o", self.tmp, "-n", "8", "-q"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        path = os.path.join(self.tmp, "bundle.bin")
        with open(path, "rb") as handle:
            data = handle.read()
        self.assertEqual(len(data), size)
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         hashlib.sha256(payload("bundle.bin", size)).hexdigest())

    def test_resume_across_runs(self):
        import subprocess as sp
        import time as time_module

        size = 3 * 1024 * 1024
        url = self.server.url("slow", "resume1.bin", size=size, delay=0.3)
        part = os.path.join(self.tmp, "resume1.bin.smlpart")

        process = sp.Popen([sys.executable, BUNDLE, url, "-o", self.tmp, "-n", "2", "-q"],
                           stdout=sp.PIPE, stderr=sp.STDOUT, text=True,
                           cwd=os.path.dirname(BUNDLE), env=isolated_env())
        try:
            deadline = time_module.time() + 30
            while time_module.time() < deadline and process.poll() is None:
                if os.path.exists(part) and os.path.getsize(part) > 0:
                    break
                time_module.sleep(0.05)
            process.terminate()  # SIGTERM: pause and keep the partial file
            process.wait(timeout=20)
        except Exception:  # pragma: no cover
            process.kill()
            raise
        finally:
            if process.stdout:
                process.stdout.close()
        self.assertTrue(os.path.exists(part), "partial file should survive SIGTERM")

        result = self.run_bundle([url, "-o", self.tmp, "-n", "2", "-q"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with open(os.path.join(self.tmp, "resume1.bin"), "rb") as handle:
            data = handle.read()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         hashlib.sha256(payload("resume1.bin", size)).hexdigest())

    def test_dry_run_works_without_the_package(self):
        url = self.server.url("meta", "huge.iso", size=6_100_000_000)
        result = self.run_bundle(["--dry-run", url])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("6.1 GB", result.stdout)

    def test_no_arguments_opens_the_window(self):
        """Double-click behaviour: no CLI arguments -> the GUI starts."""
        stub_dir = tempfile.mkdtemp(prefix="smld-stubtk-")
        self.addCleanup(shutil.rmtree, stub_dir, True)
        write_stub_tkinter(stub_dir)

        result = subprocess.run(
            [sys.executable, BUNDLE], capture_output=True, text=True,
            cwd=os.path.dirname(BUNDLE),
            env=isolated_env({"PYTHONPATH": stub_dir}),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("STUB-TK-WINDOW-CREATED", result.stdout)
        self.assertIn("STUB-TK-MAINLOOP-REACHED", result.stdout)

    def test_failure_exit_code(self):
        result = self.run_bundle([self.server.url("missing"), "-o", self.tmp, "-q"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("404", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
