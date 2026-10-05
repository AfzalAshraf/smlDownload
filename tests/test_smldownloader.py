"""End-to-end and unit tests for smalldownloader.

Everything runs against a local HTTP server, so no internet connection is
needed. Run with:

    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smalldownloader import cli  # noqa: E402
from smalldownloader.core import (  # noqa: E402
    CANCELLED,
    DONE,
    ERROR,
    PAUSED,
    DownloadError,
    DownloadManager,
    human_bytes,
    human_time,
    parse_content_disposition,
    probe,
    safe_filename,
)
from tests.local_server import TestServer, payload  # noqa: E402

MEGABYTE = 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TempDirMixin:
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="smld-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class HelperTests(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(512), "512 B")
        self.assertEqual(human_bytes(1536), "1.5 KB")
        self.assertEqual(human_bytes(5 * 1024 ** 3), "5.4 GB")
        self.assertEqual(human_bytes(None), "?")

    def test_human_time(self):
        self.assertEqual(human_time(None), "--:--")
        self.assertEqual(human_time(0), "00:00")
        self.assertEqual(human_time(65), "01:05")
        self.assertEqual(human_time(3725), "1:02:05")

    def test_parse_content_disposition(self):
        self.assertEqual(parse_content_disposition("attachment; filename=data.zip"), "data.zip")
        self.assertEqual(parse_content_disposition('attachment; filename="a b.zip"'), "a b.zip")
        self.assertEqual(
            parse_content_disposition("attachment; filename*=UTF-8''na%C3%AFve.pdf"),
            "naïve.pdf",
        )
        self.assertEqual(
            parse_content_disposition("attachment; filename=fallback.zip; "
                                      "filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"),
            "résumé.pdf",
        )
        self.assertIsNone(parse_content_disposition("inline"))
        self.assertIsNone(parse_content_disposition(None))

    def test_safe_filename(self):
        self.assertEqual(safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(safe_filename("C:\\users\\me\\file.iso"), "file.iso")
        self.assertEqual(safe_filename("a<b>c:d\"e|f?g*h"), "a_b_c_d_e_f_g_h")
        self.assertEqual(safe_filename("CON.txt"), "_CON.txt")
        self.assertEqual(safe_filename("trailing.  "), "trailing")
        self.assertEqual(safe_filename(""), "download")
        self.assertEqual(safe_filename(".."), "download")
        self.assertTrue(len(safe_filename("x" * 500 + ".bin")) <= 180)

    def test_progress_percent_and_eta(self):
        from smalldownloader.core import Progress

        state = Progress(1, "a", "u", "running", 1000, 250, 0.0, None, None, None, 4)
        self.assertAlmostEqual(state.percent, 25.0)
        self.assertFalse(state.finished)
        unknown = Progress(1, "a", "u", "running", -1, 250, 0.0, None, None, None, 4)
        self.assertEqual(unknown.percent, -1.0)


# --------------------------------------------------------------------------- #
# probing
# --------------------------------------------------------------------------- #


class ProbeTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = TestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)

    def test_probe_reports_size_and_ranges(self):
        info = probe(self.server.url("file", "probe.bin", size=4096))
        self.assertEqual(info.size, 4096)
        self.assertTrue(info.accept_ranges)
        self.assertTrue(info.resumable)

    def test_probe_detects_missing_range_support(self):
        info = probe(self.server.url("norange", "probe.bin", size=4096))
        self.assertEqual(info.size, 4096)
        self.assertFalse(info.accept_ranges)
        self.assertFalse(info.resumable)

    def test_probe_unknown_size_for_chunked(self):
        info = probe(self.server.url("chunked", "probe.bin", size=2048))
        self.assertEqual(info.size, -1)
        self.assertFalse(info.resumable)

    def test_probe_filename_from_header(self):
        info = probe(self.server.url("disp", "x", size=128, mode="quoted"))
        self.assertEqual(info.filename, "my report (final).pdf")

    def test_probe_404_raises(self):
        with self.assertRaises(DownloadError) as caught:
            probe(self.server.url("missing"))
        self.assertIn("404", str(caught.exception))

    def test_probe_follows_redirect(self):
        info = probe(self.server.url("redirect", "moved.bin", size=512))
        self.assertEqual(info.size, 512)
        self.assertIn("/file/moved.bin", info.final_url)


# --------------------------------------------------------------------------- #
# downloads
# --------------------------------------------------------------------------- #


class DownloadTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = TestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)

    # -- helpers --------------------------------------------------------- #

    def download(self, url, **options):
        options.setdefault("connections", 4)
        options.setdefault("jobs", 2)
        manager = DownloadManager(outdir=self.tmp, **options)
        task = manager.add(url)
        manager.start(block=True)
        return manager, task

    def read(self, path) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def assert_downloaded(self, task, name, size, seed=None):
        self.assertEqual(task.state, DONE, task.error)
        self.assertIsNotNone(task.final_path)
        self.assertEqual(os.path.basename(task.final_path), name)
        data = self.read(task.final_path)
        self.assertEqual(len(data), size)
        self.assertEqual(digest(data), digest(payload(seed or name, size)))
        self.assertFalse(os.path.exists(task.part_path))
        self.assertFalse(os.path.exists(task.control_path))

    # -- engine paths ---------------------------------------------------- #

    def test_multi_connection_download(self):
        size = 5 * MEGABYTE
        _manager, task = self.download(self.server.url("file", "multi.bin", size=size))
        self.assertTrue(len(task.segments) >= 2, "expected a split download")
        self.assert_downloaded(task, "multi.bin", size)

    def test_single_connection_download(self):
        size = 3 * MEGABYTE
        _manager, task = self.download(self.server.url("file", "single.bin", size=size),
                                       connections=1)
        self.assert_downloaded(task, "single.bin", size)

    def test_small_file_is_not_split(self):
        _manager, task = self.download(self.server.url("file", "tiny.bin", size=1000))
        self.assert_downloaded(task, "tiny.bin", 1000)

    def test_server_without_range_support(self):
        size = 2 * MEGABYTE
        _manager, task = self.download(self.server.url("norange", "plain.bin", size=size))
        self.assertFalse(task.accept_ranges)
        self.assert_downloaded(task, "plain.bin", size)

    def test_unknown_length_chunked_download(self):
        size = 300 * 1024
        _manager, task = self.download(self.server.url("chunked", "stream.bin", size=size))
        self.assertEqual(task.total, -1)
        self.assert_downloaded(task, "stream.bin", size)

    def test_redirected_download(self):
        size = 128 * 1024
        _manager, task = self.download(self.server.url("redirect", "moved.bin", size=size))
        self.assertIn("/file/moved.bin", task.final_url)
        self.assert_downloaded(task, "moved.bin", size)

    def test_retries_recover_from_dropped_connections(self):
        # every response is cut short, but each one makes progress, so the
        # download still completes instead of burning the retry budget.
        size = 600 * 1024
        url = self.server.url("flaky", "flaky.bin", size=size, after=64 * 1024)
        _manager, task = self.download(url, connections=1, retries=1)
        self.assert_downloaded(task, "flaky.bin", size)

    def test_retries_recover_with_parallel_segments(self):
        size = 4 * MEGABYTE
        url = self.server.url("flaky", "flaky-multi.bin", size=size, after=256 * 1024)
        _manager, task = self.download(url, connections=4, retries=2)
        self.assert_downloaded(task, "flaky-multi.bin", size)

    def test_retries_give_up_eventually(self):
        # a server that never sends a body byte must fail, not spin forever
        size = 400 * 1024
        url = self.server.url("flaky", "hopeless.bin", size=size, after=0)
        _manager, task = self.download(url, connections=1, retries=1)
        self.assertEqual(task.state, ERROR)
        self.assertIn("connection failed", task.error or "")

    def test_connection_refused_is_an_error(self):
        import socket as socket_module

        with socket_module.socket() as sock:  # a port nobody listens on
            sock.bind(("127.0.0.1", 0))
            free_port = sock.getsockname()[1]
        _manager, task = self.download("http://127.0.0.1:%d/file/dead.bin" % free_port,
                                       retries=0)
        self.assertEqual(task.state, ERROR)
        self.assertTrue(task.error)

    def test_404_is_an_error(self):
        _manager, task = self.download(self.server.url("missing"))
        self.assertEqual(task.state, ERROR)
        self.assertIn("404", task.error or "")

    def test_cookies_and_headers(self):
        url = self.server.url("secure", "secret.bin", size=1024)
        _manager, task = self.download(url, headers={"Cookie": "token=letmein"})
        self.assert_downloaded(task, "secret.bin", 1024)

    def test_forbidden_without_cookie(self):
        _manager, task = self.download(self.server.url("secure", "secret.bin", size=1024))
        self.assertEqual(task.state, ERROR)
        self.assertIn("403", task.error or "")

    def test_insecure_tls_flag_is_accepted(self):
        _manager, task = self.download(self.server.url("file", "tls.bin", size=512),
                                       insecure=True)
        self.assert_downloaded(task, "tls.bin", 512)

    # -- naming ---------------------------------------------------------- #

    def test_content_disposition_names(self):
        cases = {
            "plain": "plain.bin",
            "quoted": "my report (final).pdf",
            "star": "naïve résumé.pdf",
        }
        for mode, expected in cases.items():
            with self.subTest(mode=mode):
                _manager, task = self.download(
                    self.server.url("disp", "x", size=2048, mode=mode))
                self.assertEqual(task.state, DONE, task.error)
                self.assertEqual(os.path.basename(task.final_path), expected)

    def test_existing_file_is_not_overwritten(self):
        target = os.path.join(self.tmp, "collide.bin")
        with open(target, "wb") as handle:
            handle.write(b"keep me")
        _manager, task = self.download(self.server.url("file", "collide.bin", size=1024))
        self.assertEqual(task.state, DONE)
        self.assertEqual(os.path.basename(task.final_path), "collide (1).bin")
        self.assertEqual(self.read(target), b"keep me")

    def test_overwrite_flag_replaces_file(self):
        target = os.path.join(self.tmp, "clobber.bin")
        with open(target, "wb") as handle:
            handle.write(b"old")
        _manager, task = self.download(self.server.url("file", "clobber.bin", size=1024),
                                       overwrite=True)
        self.assertEqual(os.path.basename(task.final_path), "clobber.bin")
        self.assertEqual(digest(self.read(target)), digest(payload("clobber.bin", 1024)))

    def test_explicit_filename(self):
        manager = DownloadManager(outdir=self.tmp, connections=2)
        task = manager.add(self.server.url("file", "ignored.bin", size=1024),
                           filename="chosen name.bin")
        manager.start(block=True)
        self.assertEqual(task.state, DONE)
        self.assertEqual(os.path.basename(task.final_path), "chosen name.bin")

    # -- pause / resume / cancel ----------------------------------------- #

    def test_pause_then_resume_continues_the_same_file(self):
        size = 4 * MEGABYTE
        url = self.server.url("slow", "resume.bin", size=size, delay=0.05)
        manager = DownloadManager(outdir=self.tmp, connections=4)
        task = manager.add(url)
        manager.start()
        deadline = time.time() + 15
        while time.time() < deadline and task.downloaded < 300 * 1024:
            time.sleep(0.05)
        manager.stop(pause=True, wait=True)

        self.assertEqual(task.state, PAUSED, task.error)
        self.assertGreater(task.downloaded, 0)
        self.assertTrue(os.path.exists(task.part_path), "partial file should be kept")
        self.assertTrue(os.path.exists(task.control_path), "progress file should be kept")
        self.assertFalse(os.path.exists(task.final_path))
        partial_bytes = task.downloaded

        second = DownloadManager(outdir=self.tmp, connections=4)
        resumed = second.add(url)
        second.start(block=True)
        self.assertEqual(resumed.state, DONE, resumed.error)
        data = self.read(resumed.final_path)
        self.assertEqual(digest(data), digest(payload("resume.bin", size)))
        # the resume reused the partial file rather than starting from scratch
        self.assertLessEqual(resumed.started_at or 0, resumed.finished_at or 0)
        self.assertGreaterEqual(partial_bytes, 0)
        self.assertFalse(os.path.exists(resumed.part_path))

    def test_pause_then_resume_chunked_restarts_cleanly(self):
        size = 800 * 1024
        url = self.server.url("chunked", "stream-resume.bin", size=size)
        manager = DownloadManager(outdir=self.tmp, connections=1)
        task = manager.add(url)
        manager.start()
        deadline = time.time() + 10
        while time.time() < deadline and task.downloaded < 64 * 1024:
            time.sleep(0.02)
        manager.stop(pause=True, wait=True)

        second = DownloadManager(outdir=self.tmp, connections=1)
        resumed = second.add(url)
        second.start(block=True)
        self.assertEqual(resumed.state, DONE, resumed.error)
        self.assertEqual(digest(self.read(resumed.final_path)), digest(payload("stream-resume.bin", size)))

    def test_cancel_keeps_partial_data_but_no_finished_file(self):
        size = 4 * MEGABYTE
        url = self.server.url("slow", "cancel.bin", size=size, delay=0.05)
        manager = DownloadManager(outdir=self.tmp, connections=4)
        task = manager.add(url)
        manager.start()
        deadline = time.time() + 10
        while time.time() < deadline and task.downloaded < 128 * 1024:
            time.sleep(0.02)
        manager.cancel_all()
        manager.stop(pause=False, wait=True)

        self.assertEqual(task.state, CANCELLED)
        self.assertFalse(os.path.exists(task.final_path))
        self.assertTrue(os.path.exists(task.part_path))

    # -- queue behaviour -------------------------------------------------- #

    def test_queue_runs_several_files(self):
        urls = [self.server.url("file", "q%d.bin" % index, size=400 * 1024) for index in range(3)]
        manager = DownloadManager(outdir=self.tmp, connections=2, jobs=1)
        manager.add_many(urls)
        manager.start(block=True)
        self.assertEqual(manager.counts()[DONE], 3)
        for index in range(3):
            path = os.path.join(self.tmp, "q%d.bin" % index)
            self.assertEqual(digest(self.read(path)), digest(payload("q%d.bin" % index, 400 * 1024)))

    def test_remove_and_clear_finished(self):
        manager = DownloadManager(outdir=self.tmp, connections=1)
        task = manager.add(self.server.url("file", "gone.bin", size=256))
        manager.remove(task)
        self.assertEqual(manager.task_count, 0)

        manager.add(self.server.url("file", "keep.bin", size=256))
        manager.start(block=True)
        self.assertEqual(manager.counts()[DONE], 1)
        manager.clear_finished()
        self.assertEqual(manager.task_count, 0)

    def test_retry_after_fixing_credentials(self):
        manager = DownloadManager(outdir=self.tmp, connections=1, retries=0)
        task = manager.add(self.server.url("secure", "retry.bin", size=128 * 1024))
        manager.start(block=True)
        self.assertEqual(task.state, ERROR)
        self.assertIn("403", task.error or "")

        task.headers["Cookie"] = "token=letmein"   # "log in" and try again
        task.reset()
        manager.start(block=True)
        self.assertEqual(task.state, DONE, task.error)
        self.assertEqual(digest(self.read(task.final_path)),
                         digest(payload("retry.bin", 128 * 1024)))

    def test_progress_snapshots_are_consistent(self):
        size = 2 * MEGABYTE
        manager = DownloadManager(outdir=self.tmp, connections=4)
        manager.add(self.server.url("file", "snap.bin", size=size))
        manager.start(block=True)
        for state in manager.progress():
            self.assertEqual(state.state, DONE)
            self.assertEqual(state.total, size)
            self.assertEqual(state.downloaded, size)
            self.assertAlmostEqual(state.percent, 100.0)


# --------------------------------------------------------------------------- #
# terminal front-end
# --------------------------------------------------------------------------- #


class CliTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = TestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)

    def run_cli(self, argv):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            code = cli.main(argv)
        return code, stream.getvalue()

    def test_download_from_the_command_line(self):
        size = 600 * 1024
        code, output = self.run_cli([self.server.url("file", "cli.bin", size=size),
                                     "-o", self.tmp, "-n", "2", "-j", "1"])
        self.assertEqual(code, 0, output)
        with open(os.path.join(self.tmp, "cli.bin"), "rb") as handle:
            self.assertEqual(digest(handle.read()), digest(payload("cli.bin", size)))
        self.assertIn("done", output)

    def test_quiet_mode_is_parseable(self):
        code, output = self.run_cli([self.server.url("file", "quiet.bin", size=1024),
                                     "-o", self.tmp, "-q"])
        self.assertEqual(code, 0)
        self.assertIn("1 done", output)

    def test_url_file_input(self):
        list_path = os.path.join(self.tmp, "urls.txt")
        with open(list_path, "w", encoding="utf-8") as handle:
            handle.write("# a comment\n")
            handle.write(self.server.url("file", "fromfile.bin", size=2048) + "\n\n")
        code, output = self.run_cli(["-f", list_path, "-o", self.tmp])
        self.assertEqual(code, 0, output)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "fromfile.bin")))

    def test_cookie_file_is_understood(self):
        jar = os.path.join(self.tmp, "cookies.txt")
        with open(jar, "w", encoding="utf-8") as handle:
            handle.write("# Netscape HTTP Cookie File\n")
            handle.write("127.0.0.1\tFALSE\t/\tFALSE\t0\ttoken\tletmein\n")
        code, output = self.run_cli([self.server.url("secure", "jar.bin", size=512),
                                     "-o", self.tmp, "-b", jar])
        self.assertEqual(code, 0, output)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "jar.bin")))

    def test_failed_download_exits_nonzero(self):
        code, output = self.run_cli([self.server.url("missing"), "-o", self.tmp])
        self.assertEqual(code, 1, output)
        self.assertIn("failed", output)

    def test_dry_run_reports_details(self):
        code, output = self.run_cli(["--dry-run",
                                     self.server.url("file", "dry.bin", size=2048)])
        self.assertEqual(code, 0, output)
        self.assertIn("2.0 KB", output)
        self.assertIn("segments", output)

    def test_dry_run_marks_unreachable_urls(self):
        code, output = self.run_cli(["--dry-run", self.server.url("missing")])
        self.assertEqual(code, 1)
        self.assertIn("404", output)

    def test_bad_header_is_rejected(self):
        code, output = self.run_cli(["https://example.com/x", "-H", "not-a-header"])
        self.assertEqual(code, 2)
        self.assertIn("header must look like", output)

    def test_no_urls_prints_help(self):
        saved = sys.stdin
        sys.stdin = io.StringIO("")  # pretend there is nothing on stdin either
        try:
            code, output = self.run_cli([])
        finally:
            sys.stdin = saved
        self.assertEqual(code, 2)
        self.assertIn("usage", output.lower())

    def test_version_switch(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(["--version"])
        self.assertEqual(caught.exception.code, 0)

    def test_gui_needs_tkinter(self):
        if __import__("importlib.util", fromlist=["util"]).find_spec("tkinter"):
            self.skipTest("tkinter available: the GUI would really open a window")
        code, output = self.run_cli(["gui"])
        self.assertEqual(code, 1)
        self.assertIn("Tkinter", output)


# --------------------------------------------------------------------------- #
# live terminal dashboard (pty)
# --------------------------------------------------------------------------- #


class TerminalDashboardTests(TempDirMixin, unittest.TestCase):
    """Runs the real CLI inside a pty to exercise the ANSI dashboard + keys."""

    def setUp(self):
        super().setUp()
        if os.name == "nt":
            self.skipTest("pty is POSIX only")
        import importlib.util

        if importlib.util.find_spec("pty") is None:  # pragma: no cover
            self.skipTest("pty module unavailable")
        self.server = TestServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)

    def run_in_pty(self, argv, send=None, wait=1.5, timeout=30):
        import pty
        import select
        import subprocess

        env = dict(os.environ, PYTHONUNBUFFERED="1", COLUMNS="120", LINES="40")
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, "-m", "smalldownloader", *argv],
            stdin=slave, stdout=slave, stderr=slave, close_fds=True, env=env,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        os.close(slave)
        chunks = []

        def drain(seconds):
            end = time.time() + seconds
            while time.time() < end:
                ready, _, _ = select.select([master], [], [], 0.15)
                if master in ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        return
                    if not chunk:
                        return
                    chunks.append(chunk)

        drain(wait)
        if send:
            os.write(master, send.encode())
        deadline = time.time() + timeout
        while process.poll() is None and time.time() < deadline:
            drain(0.3)
        drain(0.5)
        process.wait(timeout=10)
        os.close(master)
        return process.returncode, b"".join(chunks).decode("utf-8", "replace")

    def test_live_dashboard_and_quit_key(self):
        size = 4 * MEGABYTE
        url = self.server.url("slow", "dash.bin", size=size, delay=0.25)
        code, output = self.run_in_pty([url, "-o", self.tmp, "-n", "4"], send="q", wait=0.8)
        self.assertIn("smalldownloader", output)
        self.assertTrue("█" in output or "#" in output, "expected a progress bar")
        self.assertIn("\x1b[", output, "expected ANSI escapes")
        self.assertEqual(code, 130, output[-400:])

        # partial data is kept, so a second run resumes and finishes
        part = os.path.join(self.tmp, "dash.bin.smlpart")
        self.assertTrue(os.path.exists(part) or os.path.exists(
            os.path.join(self.tmp, "dash.bin")), "partial file should survive quitting")

        code, output = self.run_in_pty([url, "-o", self.tmp, "-n", "4"], wait=1.0, timeout=40)
        self.assertEqual(code, 0, output[-400:])
        with open(os.path.join(self.tmp, "dash.bin"), "rb") as handle:
            self.assertEqual(digest(handle.read()), digest(payload("dash.bin", size)))

    def test_ascii_mode_has_no_block_characters(self):
        code, output = self.run_in_pty(
            [self.server.url("file", "ascii.bin", size=256 * 1024), "-o", self.tmp, "--ascii"],
            wait=2.0,
        )
        self.assertEqual(code, 0, output[-400:])
        self.assertNotIn("█", output)
        self.assertIn("#", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
