"""smalldownloader.core - the download engine.

Standard library only, no third-party dependencies.

Features
--------
* Multi-connection (segmented) downloads built on HTTP ``Range`` requests.
* Automatic fallback to a single stream when a server ignores ``Range``,
  compresses the payload, or never advertises a ``Content-Length``.
* Crash/kill safe resume: the payload lives in ``<file>.smlpart`` and the
  progress in ``<file>.smlpart.json`` - rerun the same URL to continue.
* Per-connection retries with backoff, socket timeouts, stall protection.
* Filename detection from ``Content-Disposition`` (including ``filename*``),
  Windows-safe sanitising and automatic `` (1)`` collision handling.
* Thread-safe progress snapshots (bytes, speed, ETA) for any UI.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "DownloadError",
    "DownloadTask",
    "DownloadManager",
    "ProbeInfo",
    "Progress",
    "probe",
    "safe_filename",
    "parse_content_disposition",
    "human_bytes",
    "human_time",
    "default_download_dir",
    "PENDING",
    "RUNNING",
    "PAUSED",
    "DONE",
    "ERROR",
    "CANCELLED",
]

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 smalldownloader/1.0"
)

CHUNK_SIZE = 128 * 1024  #: read/write buffer
MIN_SEGMENT_SIZE = 1024 * 1024  #: never split into pieces smaller than this

PART_SUFFIX = ".smlpart"
CONTROL_SUFFIX = ".smlpart.json"
CONTROL_VERSION = 1

# task states
PENDING = "pending"
RUNNING = "running"
PAUSED = "paused"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"

ACTIVE_STATES = (PENDING, RUNNING)
FINAL_STATES = (DONE, ERROR, CANCELLED)

_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
_WINDOWS_RESERVED.update("COM%d" % i for i in range(1, 10))
_WINDOWS_RESERVED.update("LPT%d" % i for i in range(1, 10))


class DownloadError(Exception):
    """Raised when a download cannot be completed."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def default_download_dir() -> str:
    """``~/Downloads`` when it exists, otherwise the home directory."""
    home = os.path.expanduser("~")
    downloads = os.path.join(home, "Downloads")
    return downloads if os.path.isdir(downloads) else home


def human_bytes(num: Optional[float]) -> str:
    """1536 -> ``'1.5 KB'`` (decimal units, one decimal place)."""
    if num is None or num < 0:
        return "?"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    value = float(num)
    for unit in units:
        if value < 1000 or unit == units[-1]:
            if unit == "B":
                return "%d B" % int(value)
            return "%.1f %s" % (value, unit)
        value /= 1000.0
    return "%.1f PB" % value  # pragma: no cover - unreachable


def human_time(seconds: Optional[float]) -> str:
    """3725 -> ``'1:02:05'``; ``None`` -> ``'--:--'``."""
    if seconds is None or seconds != seconds or seconds < 0 or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%02d:%02d" % (minutes, secs)


def parse_content_disposition(value: Optional[str]) -> Optional[str]:
    """Extract the filename from a ``Content-Disposition`` header."""
    if not value:
        return None
    # RFC 5987/6266: filename*=UTF-8''na%C3%AFve.zip
    match = re.search(r"filename\*\s*=\s*([^;]+)", value, re.IGNORECASE)
    if match:
        raw = match.group(1).strip().strip('"')
        if "''" in raw:
            charset, _, encoded = raw.partition("''")
            try:
                return urllib.parse.unquote(encoded, encoding=charset or "utf-8", errors="replace")
            except LookupError:
                return urllib.parse.unquote(encoded)
        return urllib.parse.unquote(raw)
    match = re.search(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', value, re.IGNORECASE)
    if match:
        return match.group(1).replace('\\"', '"')
    match = re.search(r"filename\s*=\s*([^;\s]+)", value, re.IGNORECASE)
    if match:
        return match.group(1).strip().strip('"')
    return None


def safe_filename(name: str, fallback: str = "download") -> str:
    """Turn an arbitrary remote name into something safe for a local file."""
    if not name:
        return fallback
    name = urllib.parse.unquote(name) if "%" in name else name
    name = re.split(r"[\\/]+", name)[-1]  # basename, whichever separator is used
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", name)
    name = name.strip().rstrip(".").strip()
    if not name or name in (".", ".."):
        return fallback
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    if len(name) > 180:  # keep the byte length sane on most filesystems
        root, ext = os.path.splitext(name)
        name = root[: 180 - len(ext)] + ext
    return name


def _parse_content_range(value: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """``'bytes 0-99/1000'`` -> ``(0, 99, 1000)`` (total ``-1`` when unknown)."""
    if not value:
        return None
    match = re.match(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)", value, re.IGNORECASE)
    if not match:
        return None
    total = -1 if match.group(3) == "*" else int(match.group(3))
    return int(match.group(1)), int(match.group(2)), total


def _unique_path(path: str) -> str:
    """``file.zip`` -> ``file (1).zip`` when the first name is taken."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    counter = 1
    while True:
        candidate = "%s (%d)%s" % (root, counter, ext)
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _short_error(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    return text.replace("\n", " ")[:160]


def _sleep_backoff(attempt: int) -> None:
    time.sleep(min(0.4 * attempt, 4.0))


# --------------------------------------------------------------------------- #
# probing
# --------------------------------------------------------------------------- #


@dataclass
class ProbeInfo:
    """What a server told us about a URL before downloading it."""

    url: str
    final_url: str
    filename: Optional[str]
    size: int  # -1 when unknown
    accept_ranges: bool
    etag: Optional[str]
    last_modified: Optional[str]
    status: int
    content_type: Optional[str]

    @property
    def resumable(self) -> bool:
        return self.accept_ranges and self.size >= 0


def _ssl_context(insecure: bool) -> ssl.SSLContext:
    if insecure:
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def _build_request(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    range_header: Optional[str] = None,
    method: Optional[str] = None,
    post_data: Optional[bytes] = None,
) -> urllib.request.Request:
    request = urllib.request.Request(url, data=post_data, method=method)
    sent = {key.lower() for key in (headers or {})}
    if "user-agent" not in sent:
        request.add_header("User-Agent", user_agent)
    if "accept" not in sent:
        request.add_header("Accept", "*/*")
    if "accept-encoding" not in sent:
        request.add_header("Accept-Encoding", "identity")  # byte offsets stay valid
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    if range_header:
        request.add_header("Range", range_header)
    return request


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    message = "HTTP %s %s" % (exc.code, exc.reason)
    if exc.code in (401, 403):
        message += " (try passing cookies/headers: -b or -H)"
    return message


def probe(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 30.0,
    insecure: bool = False,
    method: Optional[str] = None,
    post_data: Optional[bytes] = None,
) -> ProbeInfo:
    """Ask a server for size / range support / filename without downloading it."""
    context = _ssl_context(insecure)

    def open_request(range_header: Optional[str], use_method: Optional[str] = None):
        request = _build_request(
            url,
            headers=headers,
            user_agent=user_agent,
            range_header=range_header,
            method=use_method,
            post_data=post_data,
        )
        return urllib.request.urlopen(request, timeout=timeout, context=context)

    def describe(response, ranges: bool, size: int) -> ProbeInfo:
        return ProbeInfo(
            url=url,
            final_url=response.geturl(),
            filename=parse_content_disposition(response.headers.get("Content-Disposition")),
            size=size,
            accept_ranges=ranges,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            status=response.status,
            content_type=response.headers.get("Content-Type"),
        )

    def usable_size(response) -> int:
        encoding = (response.headers.get("Content-Encoding") or "identity").lower()
        if encoding not in ("identity", ""):
            return -1
        length = response.headers.get("Content-Length")
        return int(length) if length and length.isdigit() else -1

    # 1) HEAD: cheapest probe, but plenty of servers dislike it.
    try:
        with open_request(None, "HEAD") as response:
            size = usable_size(response)
            if size >= 0:
                accept = (response.headers.get("Accept-Ranges") or "").lower() == "bytes"
                return describe(response, accept, size)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404, 410):
            raise DownloadError(_http_error_message(exc)) from None
    except Exception:
        pass

    # 2) one-byte ranged GET: reveals both the size and the range support.
    try:
        with open_request("bytes=0-0") as response:
            if response.status == 206:
                parsed = _parse_content_range(response.headers.get("Content-Range"))
                if parsed and parsed[2] >= 0:
                    return describe(response, True, parsed[2])
            else:
                return describe(response, False, usable_size(response))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404, 410):
            raise DownloadError(_http_error_message(exc)) from None
    except Exception:
        pass

    # 3) plain GET with the body thrown away.
    try:
        with open_request(None) as response:
            return describe(response, False, usable_size(response))
    except urllib.error.HTTPError as exc:
        raise DownloadError(_http_error_message(exc)) from None


# --------------------------------------------------------------------------- #
# progress snapshot
# --------------------------------------------------------------------------- #


@dataclass
class Progress:
    """Snapshot of a task, safe to hand to a UI from any thread."""

    id: int
    name: str
    url: str
    state: str
    total: int
    downloaded: int
    speed: float
    eta: Optional[float]
    error: Optional[str]
    dest: Optional[str]
    connections: int

    @property
    def percent(self) -> float:
        """0-100, or ``-1`` when the total size is unknown."""
        if self.total <= 0:
            return -1.0
        return min(100.0, self.downloaded * 100.0 / self.total)

    @property
    def finished(self) -> bool:
        return self.state in FINAL_STATES


# --------------------------------------------------------------------------- #
# control file helpers
# --------------------------------------------------------------------------- #


def _write_control(path: str, payload: Dict) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _read_control(path: str) -> Optional[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("v") == CONTROL_VERSION:
        return data
    return None


# --------------------------------------------------------------------------- #
# task
# --------------------------------------------------------------------------- #


class DownloadTask:
    """One URL to fetch, plus its live progress and resume state."""

    _ids = itertools.count(1)

    def __init__(
        self,
        url: str,
        outdir: str,
        filename: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        connections: int = 4,
        retries: int = 3,
        resume: bool = True,
        overwrite: bool = False,
        insecure: bool = False,
        timeout: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
        method: Optional[str] = None,
        post_data: Optional[bytes] = None,
    ) -> None:
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise DownloadError("not an http(s) URL: %s" % url)
        self.id = next(DownloadTask._ids)
        self.url = url
        self.outdir = os.path.abspath(os.path.expanduser(outdir))
        self.requested_name = filename
        self.headers = dict(headers or {})
        self.connections = max(1, int(connections))
        self.retries = max(0, int(retries))
        self.resume = bool(resume)
        self.overwrite = bool(overwrite)
        self.insecure = bool(insecure)
        self.timeout = float(timeout)
        self.user_agent = user_agent
        self.method = method
        self.post_data = post_data

        self.state = PENDING
        self.error: Optional[str] = None
        self.total = -1
        self.downloaded = 0
        self.speed = 0.0
        self.eta: Optional[float] = None
        self.final_path: Optional[str] = None
        self.filename = filename or ""
        self.etag: Optional[str] = None
        self.last_modified: Optional[str] = None
        self.accept_ranges = False
        self.final_url: Optional[str] = None
        self.segments: List[List[int]] = []
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

        # internals
        self.lock = threading.RLock()
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._segment_abort = threading.Event()
        self._control_lock = threading.Lock()
        self._samples: List[Tuple[float, int]] = []
        self._last_activity = time.time()
        self._last_control_save = 0.0
        self._file = None

    # -- names and paths --------------------------------------------------- #

    @property
    def part_path(self) -> str:
        return (self.final_path or os.path.join(self.outdir, "download")) + PART_SUFFIX

    @property
    def control_path(self) -> str:
        return self.part_path + ".json"

    @property
    def name(self) -> str:
        if self.final_path:
            return os.path.basename(self.final_path)
        if self.filename:
            return self.filename
        if self.requested_name:
            return self.requested_name
        base = os.path.basename(urllib.parse.urlparse(self.url).path)
        return urllib.parse.unquote(base) if base else self.url

    # -- state ------------------------------------------------------------- #

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def is_finished(self) -> bool:
        return self.state in FINAL_STATES

    def progress(self) -> Progress:
        with self.lock:
            return Progress(
                id=self.id,
                name=self.name,
                url=self.url,
                state=self.state,
                total=self.total,
                downloaded=self.downloaded,
                speed=self.speed,
                eta=self.eta,
                error=self.error,
                dest=self.final_path,
                connections=self.connections,
            )

    def sample(self) -> None:
        """Refresh speed/ETA from the byte counter (called by the manager ticker)."""
        now = time.time()
        with self.lock:
            self._samples.append((now, self.downloaded))
            cutoff = now - 4.0
            while len(self._samples) > 2 and self._samples[0][0] < cutoff:
                self._samples.pop(0)
            if len(self._samples) >= 2:
                t0, b0 = self._samples[0]
                t1, b1 = self._samples[-1]
                self.speed = max(0.0, (b1 - b0) / max(t1 - t0, 1e-6))
            else:
                self.speed = 0.0
            if self.state != RUNNING or now - self._last_activity > 3.0:
                self.speed = 0.0  # stalled or finished: don't show a happy fake speed
            if self.speed > 1 and self.total > 0 and self.downloaded < self.total:
                self.eta = (self.total - self.downloaded) / self.speed
            else:
                self.eta = None

    # -- user controls ----------------------------------------------------- #

    def cancel(self) -> None:
        self._cancel.set()
        self._pause.set()

    def pause(self) -> None:
        self._pause.set()

    def reset(self) -> None:
        """Queue the task again, keeping resumable ``.smlpart`` data."""
        self._cancel.clear()
        self._pause.clear()
        self._segment_abort.clear()
        with self.lock:
            self.state = PENDING
            self.error = None
            self.speed = 0.0
            self.eta = None
            self._samples = []

    # -- HTTP -------------------------------------------------------------- #

    def _open(self, range_header: Optional[str] = None, timeout: Optional[float] = None):
        request = _build_request(
            self.url,
            headers=self.headers,
            user_agent=self.user_agent,
            range_header=range_header,
            method=self.method,
            post_data=self.post_data,
        )
        return urllib.request.urlopen(
            request, timeout=timeout or self.timeout, context=_ssl_context(self.insecure)
        )

    def _do_probe(self) -> ProbeInfo:
        info = probe(
            self.url,
            headers=self.headers,
            user_agent=self.user_agent,
            timeout=self.timeout,
            insecure=self.insecure,
            method=self.method,
            post_data=self.post_data,
        )
        self.etag = info.etag
        self.last_modified = info.last_modified
        self.final_url = info.final_url
        self.accept_ranges = info.accept_ranges
        if (self.method or "GET").upper() not in ("GET", "HEAD"):
            self.accept_ranges = False  # ranged POST/PUT is not something we trust
        name = self.requested_name or info.filename or self._name_from_url(info.final_url)
        self.filename = safe_filename(name)
        return info

    def _name_from_url(self, url: str) -> str:
        base = os.path.basename(urllib.parse.urlparse(url).path)
        return safe_filename(base) if base else ""

    def _resolve_paths(self) -> None:
        """Pick ``final_path``, reusing a resumable part file when there is one."""
        os.makedirs(self.outdir, exist_ok=True)
        candidate = os.path.join(self.outdir, self.filename or "download")
        part, control = candidate + PART_SUFFIX, candidate + CONTROL_SUFFIX
        if self.resume and os.path.exists(part) and os.path.exists(control):
            state = _read_control(control)
            if state and state.get("url") == self.url:
                self.final_path = candidate
                return
        if os.path.exists(candidate) and not self.overwrite:
            candidate = _unique_path(candidate)
        self.final_path = candidate

    # -- control file ------------------------------------------------------ #

    def _save_control(self, force: bool = False) -> None:
        if not self.final_path:
            return
        now = time.time()
        if not force and now - self._last_control_save < 1.0:
            return
        with self.lock:
            self._last_control_save = now
            payload = {
                "v": CONTROL_VERSION,
                "url": self.url,
                "name": self.name,
                "total": self.total,
                "etag": self.etag,
                "last_modified": self.last_modified,
                "accept_ranges": self.accept_ranges,
                "segments": [list(segment) for segment in self.segments],
                "updated": now,
            }
        with self._control_lock:
            _write_control(self.control_path, payload)

    def _load_control(self) -> bool:
        """Restore previous progress; ``True`` when the part file is usable."""
        if not (self.resume and self.final_path and os.path.exists(self.part_path)):
            return False
        state = _read_control(self.control_path)
        if not state or state.get("url") != self.url:
            return False
        if state.get("total", -1) != self.total and state.get("total", -1) > 0 and self.total > 0:
            return False  # remote file changed size
        if state.get("etag") and self.etag and state["etag"] != self.etag:
            return False
        if not state.get("etag") and state.get("last_modified") and self.last_modified:
            if state["last_modified"] != self.last_modified:
                return False
        segments = state.get("segments") or []
        if not segments:
            return False
        self.segments = [[int(s), int(e), int(d)] for s, e, d in segments]
        self.downloaded = sum(min(seg[2], seg[1] - seg[0] + 1) for seg in self.segments)
        if state.get("total", -1) > 0:
            self.total = int(state["total"])
        return True

    def _discard_partial(self) -> None:
        for path in (self.control_path, self.part_path):
            try:
                os.remove(path)
            except OSError:
                pass
        self.segments = []
        self.downloaded = 0

    # -- payload file ------------------------------------------------------ #

    def _open_part(self, size: int, fresh: bool, preallocate: bool = True) -> None:
        """Open the payload file; *fresh* truncates it first.

        ``preallocate`` extends a fresh file to the final size so parallel
        writers can seek to their own offsets straight away.
        """
        if fresh:
            self._file = open(self.part_path, "wb")
            if preallocate and size > 0:
                self._file.truncate(size)
        else:
            self._file = open(self.part_path, "r+b")

    def _close_part(self) -> None:
        if self._file is None:
            return
        try:
            self._file.close()
        finally:
            self._file = None

    def _write_at(self, offset: int, chunk: bytes) -> None:
        with self.lock:
            self._file.seek(offset)
            self._file.write(chunk)
            self._last_activity = time.time()

    # -- the actual transfers --------------------------------------------- #

    def _download_segment(self, index: int) -> None:
        """Fetch one byte range, retrying the remainder until it is complete.

        The retry budget counts *consecutive attempts without progress*, so a
        flaky server that keeps dropping the connection still finishes - while a
        server that hands out nothing at all fails quickly. A hard attempt cap
        keeps a byte-dribbling server from running forever.
        """
        segment = self.segments[index]
        failures = 0
        attempts = 0
        max_attempts = 100 + 25 * self.retries
        while True:
            if self._cancel.is_set() or self._pause.is_set() or self._segment_abort.is_set():
                return
            start = segment[0] + segment[2]
            if start > segment[1]:
                return
            before = segment[2]
            attempts += 1
            error: Optional[BaseException] = None
            try:
                with self._open("bytes=%d-%d" % (start, segment[1])) as response:
                    status = response.status
                    if status == 416:  # the server considers this range complete
                        with self.lock:
                            self.downloaded += max(0, segment[1] - segment[0] + 1 - segment[2])
                            segment[2] = segment[1] - segment[0] + 1
                        return
                    if status == 200:
                        # Range support vanished mid-flight: restart as one stream.
                        self._segment_abort.set()
                        return
                    if status != 206:
                        raise DownloadError("unexpected HTTP status %s" % status)
                    offset = start
                    while True:
                        if self._cancel.is_set() or self._pause.is_set():
                            return
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        self._write_at(offset, chunk)
                        offset += len(chunk)
                        with self.lock:
                            segment[2] = offset - segment[0]
                            self.downloaded += len(chunk)
                        self._save_control()
            except (urllib.error.URLError, urllib.error.HTTPError, ssl.SSLError,
                    socket.timeout, OSError, DownloadError, ValueError) as exc:
                if self._cancel.is_set() or self._pause.is_set():
                    return
                error = exc
            if segment[0] + segment[2] > segment[1]:
                return
            failures = 0 if segment[2] > before else failures + 1
            if error is None:
                error = DownloadError("server closed the connection early")
            if failures > self.retries or attempts >= max_attempts:
                raise DownloadError(
                    "connection failed after %d attempt(s): %s"
                    % (attempts, _short_error(error))
                ) from error
            _sleep_backoff(max(failures, 1))

    def _consume_stream(self, response, offset: int) -> int:
        while True:
            if self._cancel.is_set() or self._pause.is_set():
                return offset
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                return offset
            self._write_at(offset, chunk)
            offset += len(chunk)
            with self.lock:
                self.downloaded = offset
                self.segments = [[0, max(offset - 1, 0), offset]]
            self._save_control()

    def _download_single(self, expected: int) -> None:
        """One stream; resumes with an open-ended ``Range`` when the server allows it.

        A saved offset is only ever trusted when the server honours ranges -
        otherwise we must start at byte 0 to keep the bytes aligned.
        """
        offset = 0
        if self.resume and self.accept_ranges and os.path.exists(self.part_path):
            try:
                offset = os.path.getsize(self.part_path)
            except OSError:
                offset = 0
            if expected > 0 and offset >= expected:
                offset = 0  # nothing left to fetch -> restart cleanly
        self._open_part(expected, fresh=offset == 0, preallocate=False)
        failures = 0
        attempts = 0
        max_attempts = 100 + 25 * self.retries
        while True:
            if self._cancel.is_set() or self._pause.is_set():
                return
            before = offset
            attempts += 1
            error: Optional[BaseException] = None
            try:
                range_header = "bytes=%d-" % offset if (offset > 0 and self.accept_ranges) else None
                with self._open(range_header) as response:
                    if range_header and response.status != 206:
                        # Server ignored the range: start over so bytes stay aligned.
                        self._close_part()
                        self._open_part(expected, fresh=True, preallocate=False)
                        offset = 0
                        before = 0
                        self.downloaded = 0
                    offset = self._consume_stream(response, offset)
            except (urllib.error.URLError, urllib.error.HTTPError, ssl.SSLError,
                    socket.timeout, OSError, DownloadError, ValueError) as exc:
                if self._cancel.is_set() or self._pause.is_set():
                    return
                error = exc
            self.downloaded = offset
            if expected < 0 or offset >= expected:
                return
            failures = 0 if offset > before else failures + 1
            if error is None:
                error = DownloadError(
                    "download stopped early at %s of %s bytes"
                    % (human_bytes(offset), human_bytes(expected))
                )
            if failures > self.retries or attempts >= max_attempts:
                raise DownloadError(
                    "connection failed after %d attempt(s): %s"
                    % (attempts, _short_error(error))
                ) from error
            _sleep_backoff(max(failures, 1))

    # -- orchestration ----------------------------------------------------- #

    def run(self) -> None:
        """Download the file (blocking). Failures land in :attr:`state`/``error``."""
        self.started_at = time.time()
        self.finished_at = None
        self.error = None
        self._segment_abort.clear()
        with self.lock:
            self.state = RUNNING
        try:
            self._run()
        except DownloadError as exc:
            self._fail(str(exc))
        except Exception as exc:  # unexpected but still surfaced to the user
            self._fail("%s: %s" % (type(exc).__name__, exc))
        finally:
            self._close_part()
            if self.state in (PAUSED, CANCELLED, ERROR) and self.final_path:
                self._save_control(force=True)
            self.finished_at = time.time()
            with self.lock:
                self.speed = 0.0
                self.eta = None

    def _run(self) -> None:
        info = self._do_probe()
        self._resolve_paths()

        if self._cancel.is_set() or self._pause.is_set():
            self._stop_state()
            return

        self.total = info.size
        resumed = self._load_control()
        if resumed and len(self.segments) > 1 and not info.resumable:
            self._discard_partial()
            resumed = False
        if not resumed and not info.resumable:
            self._discard_partial()  # a plain stream cannot be resumed safely
        segmented = (
            info.resumable
            and self.connections > 1
            and self.total > 0
            and (self.method or "GET").upper() == "GET"
        )
        if segmented and not resumed:
            self._plan_segments(self.total)

        if self._cancel.is_set() or self._pause.is_set():
            self._stop_state()
            return

        if segmented:
            self._run_segmented()
        else:
            self._download_single(self.total if self.total > 0 else -1)

        if self._cancel.is_set() or self._pause.is_set():
            self._stop_state()
            return

        if self._is_complete():
            self._finalize()
        else:
            self._fail(
                "incomplete download (%s of %s)"
                % (
                    human_bytes(self.downloaded),
                    human_bytes(self.total) if self.total > 0 else "unknown size",
                )
            )

    def _stop_state(self) -> None:
        with self.lock:
            self.state = CANCELLED if self._cancel.is_set() else PAUSED

    def _plan_segments(self, total: int) -> None:
        count = max(1, min(self.connections, int(math.ceil(total / float(MIN_SEGMENT_SIZE)))))
        base = total // count
        self.segments = []
        position = 0
        for index in range(count):
            end = total - 1 if index == count - 1 else position + base - 1
            self.segments.append([position, end, 0])
            position = end + 1
        self.downloaded = 0

    def _run_segmented(self) -> None:
        used_part = os.path.exists(self.part_path) and bool(self.segments)
        self._open_part(self.total, fresh=not used_part)
        self._save_control(force=True)
        workers = [
            threading.Thread(target=self._download_segment, args=(index,), daemon=True)
            for index in range(len(self.segments))
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        if self._segment_abort.is_set() and not (self._cancel.is_set() or self._pause.is_set()):
            # Range support disappeared: close, wipe and redo it as one stream.
            self._close_part()
            self._discard_partial()
            self.accept_ranges = False
            self._download_single(self.total if self.total > 0 else -1)

    def _is_complete(self) -> bool:
        if self.total > 0:
            if self.segments:
                return all(seg[2] >= seg[1] - seg[0] + 1 for seg in self.segments)
            return self.downloaded >= self.total
        # unknown length: a clean end-of-stream is all we have to go on
        return not self.error

    def _finalize(self) -> None:
        self._close_part()
        if self.total > 0:
            try:
                if os.path.getsize(self.part_path) != self.total:
                    raise DownloadError(
                        "finished file has %s bytes, expected %s"
                        % (os.path.getsize(self.part_path), self.total)
                    )
            except OSError as exc:
                raise DownloadError("cannot inspect the finished file: %s" % exc) from exc
        try:
            os.replace(self.part_path, self.final_path)
        except OSError as exc:
            raise DownloadError("could not move the finished file into place: %s" % exc) from exc
        try:
            os.remove(self.control_path)
        except OSError:
            pass
        with self.lock:
            if self.total > 0:
                self.downloaded = self.total
            self.state = DONE

    def _fail(self, message: str) -> None:
        with self.lock:
            self.error = message
            self.state = ERROR


# --------------------------------------------------------------------------- #
# manager
# --------------------------------------------------------------------------- #


class DownloadManager:
    """A tiny queue: ``jobs`` files at a time, ``connections`` per file."""

    def __init__(
        self,
        outdir: Optional[str] = None,
        connections: int = 4,
        jobs: int = 2,
        retries: int = 3,
        headers: Optional[Dict[str, str]] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        resume: bool = True,
        overwrite: bool = False,
        insecure: bool = False,
        timeout: float = 30.0,
        method: Optional[str] = None,
        post_data: Optional[bytes] = None,
        on_update: Optional[Callable[["DownloadManager"], None]] = None,
    ) -> None:
        self.outdir = os.path.abspath(os.path.expanduser(outdir or default_download_dir()))
        self.connections = max(1, int(connections))
        self.jobs = max(1, int(jobs))
        self.retries = max(0, int(retries))
        self.headers = dict(headers or {})
        self.user_agent = user_agent
        self.resume = bool(resume)
        self.overwrite = bool(overwrite)
        self.insecure = bool(insecure)
        self.timeout = float(timeout)
        self.method = method
        self.post_data = post_data
        self.on_update = on_update
        self.tasks: List[DownloadTask] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ticker_stop = threading.Event()
        self._runner: Optional[threading.Thread] = None
        self._ticker: Optional[threading.Thread] = None

    # -- queue ------------------------------------------------------------- #

    def add(self, url: str, filename: Optional[str] = None, **overrides) -> DownloadTask:
        options = dict(
            headers=self.headers,
            connections=self.connections,
            retries=self.retries,
            resume=self.resume,
            overwrite=self.overwrite,
            insecure=self.insecure,
            timeout=self.timeout,
            user_agent=self.user_agent,
            method=self.method,
            post_data=self.post_data,
        )
        options.update(overrides)
        task = DownloadTask(url, self.outdir, filename=filename, **options)
        with self._lock:
            self.tasks.append(task)
        self._notify()
        return task

    def add_many(self, urls: Iterable[str]) -> List[DownloadTask]:
        added = []
        for url in urls:
            text = str(url).strip()
            if text:
                added.append(self.add(text))
        return added

    def remove(self, task: DownloadTask) -> None:
        if task.is_active:
            task.cancel()
        with self._lock:
            if task in self.tasks:
                self.tasks.remove(task)
        self._notify()

    def clear_finished(self) -> None:
        with self._lock:
            self.tasks = [task for task in self.tasks if not task.is_finished]
        self._notify()

    # -- controls ---------------------------------------------------------- #

    def start(self, block: bool = False) -> None:
        """Kick off (or resume) the queue; returns immediately unless *block*."""
        for task in self.tasks:
            if task.state == PAUSED:
                task.reset()
        self._stop.clear()
        with self._lock:
            if self._runner and self._runner.is_alive():
                runner = self._runner
            else:
                runner = threading.Thread(target=self.run, name="smld-runner", daemon=True)
                self._runner = runner
                runner.start()
        if block:
            runner.join()

    def run(self) -> None:
        """Scheduler loop: until the queue drains or nothing is left to do."""
        self._start_ticker()
        running: Dict[int, threading.Thread] = {}
        try:
            while True:
                for task_id, thread in list(running.items()):
                    if not thread.is_alive():
                        running.pop(task_id)
                if self._stop.is_set():
                    break
                pending = []
                waiting = False
                for task in list(self.tasks):
                    if task._cancel.is_set() and task.state == PENDING:
                        task.state = CANCELLED
                    elif task.state == PENDING and not task._pause.is_set():
                        pending.append(task)
                    elif task.state == PAUSED:
                        waiting = True
                if not pending and not running:
                    if waiting and not self._stop.is_set():
                        time.sleep(0.15)
                        continue
                    break
                while pending and len(running) < self.jobs and not self._stop.is_set():
                    task = pending.pop(0)
                    thread = threading.Thread(
                        target=task.run, name="smld-task-%d" % task.id, daemon=True
                    )
                    running[task.id] = thread
                    thread.start()
                time.sleep(0.12)
        finally:
            for task in self.tasks:
                if task.is_active and not task._cancel.is_set():
                    task.pause()
            for thread in running.values():
                thread.join()
            for task in self.tasks:
                if task.state == RUNNING:
                    task.state = PAUSED
            self._stop_ticker()
            self._notify()

    def stop(self, pause: bool = True, wait: bool = True) -> None:
        """Stop the queue. Pauses (resumable) by default, cancels otherwise."""
        for task in self.tasks:
            if task.is_active:
                task.pause() if pause else task.cancel()
        self._stop.set()
        runner = self._runner
        if wait and runner and runner.is_alive() and runner is not threading.current_thread():
            runner.join()

    def pause_all(self) -> None:
        for task in self.tasks:
            if task.is_active:
                task.pause()
        self._notify()

    def resume_all(self) -> None:
        self.start()

    def cancel_all(self) -> None:
        for task in self.tasks:
            task.cancel()
        self._notify()

    def retry(self, task: DownloadTask) -> None:
        task.reset()
        self.start()

    # -- introspection ----------------------------------------------------- #

    def progress(self) -> List[Progress]:
        with self._lock:
            tasks = list(self.tasks)
        return [task.progress() for task in tasks]

    @property
    def task_count(self) -> int:
        with self._lock:
            return len(self.tasks)

    def is_running(self) -> bool:
        return bool(self._runner and self._runner.is_alive())

    @property
    def total_speed(self) -> float:
        with self._lock:
            return sum(task.speed for task in self.tasks if task.state == RUNNING)

    def counts(self) -> Dict[str, int]:
        counts = {PENDING: 0, RUNNING: 0, PAUSED: 0, DONE: 0, ERROR: 0, CANCELLED: 0}
        with self._lock:
            for task in self.tasks:
                counts[task.state] = counts.get(task.state, 0) + 1
        return counts

    # -- internals --------------------------------------------------------- #

    def _notify(self) -> None:
        if self.on_update:
            try:
                self.on_update(self)
            except Exception:
                pass

    def _start_ticker(self) -> None:
        if self._ticker and self._ticker.is_alive():
            return
        self._ticker_stop.clear()

        def tick() -> None:
            while not self._ticker_stop.is_set():
                with self._lock:
                    tasks = list(self.tasks)
                for task in tasks:
                    if task.state in (RUNNING, PENDING):
                        task.sample()
                self._notify()
                self._ticker_stop.wait(0.25)

        self._ticker = threading.Thread(target=tick, name="smld-ticker", daemon=True)
        self._ticker.start()

    def _stop_ticker(self) -> None:
        self._ticker_stop.set()
        ticker = self._ticker
        if ticker and ticker.is_alive() and ticker is not threading.current_thread():
            ticker.join(timeout=1.0)

    def __enter__(self) -> "DownloadManager":
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop(wait=True)
