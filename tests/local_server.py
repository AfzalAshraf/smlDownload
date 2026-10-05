"""A small HTTP server used by the test-suite to act like a real download host.

Routes (all before any query parsing):
    /file/<name>?size=N          ranges supported, Content-Length, ETag
    /norange/<name>?size=N       no Accept-Ranges, ignores Range requests
    /chunked/<name>?size=N       no Content-Length (Transfer-Encoding: chunked)
    /slow/<name>?size=N&delay=D  ranges supported, throttled
    /flaky/<name>?size=N&after=M supports ranges but drops the connection after M bytes
    /abort                       hangs up without sending a response at all
    /meta/<name>?size=N          headers only - pretend to be any size (docs/demos)
    /disp/<name>?size=N&mode=... proves Content-Disposition filename handling
    /redirect/<name>?size=N      302 -> /file/...
    /secure/<name>?size=N        needs Cookie: token=letmein
    /missing                     always 404
"""

from __future__ import annotations

import hashlib
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse


WINDOW = 1 << 20  # 1 MiB window: lets the server fake files of any size


def window(name: str, index: int) -> bytes:
    """Deterministic 1 MiB block; byte *i* of a file depends only on name/i."""
    return random.Random("smalldownloader:%s:%d" % (name, index)).randbytes(WINDOW)


def payload(name: str, size: int) -> bytes:
    """Deterministic bytes for a file (tests use small sizes)."""
    parts = []
    remaining = size
    index = 0
    while remaining > 0:
        take = min(remaining, WINDOW)
        parts.append(window(name, index)[:take])
        remaining -= take
        index += 1
    return b"".join(parts)


def chunks(name: str, start: int, end: int):
    """Yield the bytes of ``[start, end]`` (inclusive) window by window."""
    index = start // WINDOW
    offset = start % WINDOW
    position = start
    while position <= end:
        block = window(name, index)[offset:]
        take = min(len(block), end - position + 1)
        yield block[:take]
        position += take
        index += 1
        offset = 0


def etag(name: str, size: int) -> str:
    digest = hashlib.sha256(("%s:%d" % (name, size)).encode()).hexdigest()[:16]
    return '"%s"' % digest


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "smld-test/1.0"

    def log_message(self, *args):  # silence
        pass

    # -- helpers ------------------------------------------------------- #

    def _params(self, path: str):
        parsed = urlparse(path)
        query = parse_qs(parsed.query)
        name = unquote(parsed.path.split("/", 2)[-1]) or "data.bin"
        size = int(query.get("size", ["65536"])[0])
        return name, size, query

    def _range(self, size: int):
        header = self.headers.get("Range")
        if not header:
            return None
        match = re.match(r"bytes=(\d+)-(\d*)", header)
        if not match:
            return None
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
        return start, min(end, size - 1)

    def _send(self, body: bytes, status: int = 200, extra=None, ranges=False, size=None):
        self.send_response(status)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- verbs ---------------------------------------------------------- #

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only: bool = False):
        path = self.path
        if path.startswith("/missing"):
            self.send_error(404, "Not Found")
            return
        if path.startswith("/meta/"):
            # headers only: lets demos pretend to be a multi-gigabyte file
            name, size, query = self._params(path)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            if query.get("ranges", ["1"])[0] != "0":
                self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag(name, size))
            self.send_header("Last-Modified", "Wed, 01 Jan 2025 00:00:00 GMT")
            if query.get("disp"):
                self.send_header("Content-Disposition",
                                 'attachment; filename="%s"' % query["disp"][0])
            self.end_headers()
            if not head_only:
                try:
                    self.wfile.write(chunks(name, 0, min(size, 4096) - 1).__next__())
                except (StopIteration, BrokenPipeError, ConnectionResetError):
                    pass
            return
        if path.startswith("/abort"):
            # hang up without answering a single byte
            self.close_connection = True
            try:
                self.connection.shutdown(2)
            except OSError:
                pass
            try:
                self.connection.close()
            except OSError:
                pass
            return
        if path.startswith("/redirect/"):
            name, size, _ = self._params(path)
            self.send_response(302)
            self.send_header("Location", "/file/%s?size=%d" % (name, size))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/secure/"):
            if "letmein" not in (self.headers.get("Cookie") or ""):
                self.send_error(403, "Forbidden")
                return
            name, size, _ = self._params(path)
            self._file(name, size, head_only)
            return
        if path.startswith("/disp/"):
            name, size, query = self._params(path)
            mode = query.get("mode", ["plain"])[0]
            if mode == "star":
                disposition = "attachment; filename*=UTF-8''na%C3%AFve%20r%C3%A9sum%C3%A9.pdf"
            elif mode == "quoted":
                disposition = 'attachment; filename="my report (final).pdf"'
            else:
                disposition = "attachment; filename=plain.bin"
            body = payload(name, size)
            ranges = mode != "norange"
            self._send(body, extra={"Content-Disposition": disposition,
                                    "Content-Type": "application/octet-stream",
                                    "ETag": etag(name, size)}, ranges=ranges)
            return

        name, size, query = self._params(path)
        if path.startswith("/norange/"):
            self._plain(name, size, head_only, ranges=False)
        elif path.startswith("/chunked/"):
            self._chunked(name, size)
        elif path.startswith("/slow/"):
            delay = float(query.get("delay", ["0.02"])[0])
            self._file(name, size, head_only, delay=delay)
        elif path.startswith("/flaky/"):
            after = int(query.get("after", ["1024"])[0])
            self._flaky(name, size, after)
        else:
            self._file(name, size, head_only)

    # -- bodies --------------------------------------------------------- #

    def _plain(self, name: str, size: int, head_only: bool, ranges: bool = False,
               delay: float = 0.0, end: int = -1):
        """One unchunked body; ``ranges`` only advertises support (ignores it)."""
        last = size - 1 if end < 0 else min(end, size - 1)
        partial = end >= 0
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("ETag", etag(name, size))
        self.send_header("Last-Modified", "Wed, 01 Jan 2025 00:00:00 GMT")
        if partial:
            self.send_header("Content-Range", "bytes 0-%d/%d" % (last, size))
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(last + 1))
        self.end_headers()
        if head_only:
            return
        try:
            for piece in chunks(name, 0, last):
                self.wfile.write(piece)
                if delay:
                    self.wfile.flush()
                    time.sleep(delay)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _file(self, name: str, size: int, head_only: bool = False, delay: float = 0.0):
        rng = self._range(size)
        extra = {"Content-Type": "application/octet-stream",
                 "ETag": etag(name, size),
                 "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"}
        if rng:
            start, end = rng
            extra["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
            if head_only:
                self.send_response(206)
                for key, value in extra.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                return
            self.send_response(206)
            for key, value in extra.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            try:
                self._write_range(name, start, end, delay)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if head_only:
            self.send_response(200)
            for key, value in extra.items():
                self.send_header(key, value)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            return
        self.send_response(200)
        for key, value in extra.items():
            self.send_header(key, value)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        try:
            self._write_range(name, 0, size - 1, delay)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _write_range(self, name: str, start: int, end: int, delay: float = 0.0) -> None:
        """Stream ``[start, end]``, one window at a time (never a huge buffer)."""
        piece_count = max(1, min(20, (end - start) // max(WINDOW // 20, 1) + 1))
        step = max(1, (end - start + 1) // piece_count)
        position = start
        while position <= end:
            stop = min(end, position + step - 1)
            for block in chunks(name, position, stop):
                self.wfile.write(block)
            position = stop + 1
            if delay:
                self.wfile.flush()
                time.sleep(delay)

    def _chunked(self, name: str, size: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for block in chunks(name, 0, size - 1):
                for offset in range(0, len(block), 8192):
                    piece = block[offset:offset + 8192]
                    self.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _flaky(self, name: str, size: int, after: int) -> None:
        """Hand back part of the requested range and slam the connection shut."""
        rng = self._range(size)
        start, end = rng if rng else (0, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "application/octet-stream")
        if rng:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if after > 0:
            try:
                for block in chunks(name, start, min(end, start + after - 1)):
                    self.wfile.write(block)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            self.connection.close()
        except OSError:
            pass


class TestServer:
    """Context manager that runs :class:`Handler` on a background thread."""

    def __init__(self, port: int = 0) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def url(self, route: str, name: str = "data.bin", **query) -> str:
        params = "&".join("%s=%s" % (key, value) for key, value in query.items())
        return "%s/%s/%s%s" % (self.base, route, name, ("?" + params) if params else "")

    def __enter__(self) -> "TestServer":
        self.thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


if __name__ == "__main__":  # manual playground: python tests/local_server.py
    with TestServer() as server:
        print("serving on", server.base)
        print("  ", server.url("file", "big.bin", size=5 * 1024 * 1024))
        try:
            time.sleep(600)
        except KeyboardInterrupt:
            print("bye")
