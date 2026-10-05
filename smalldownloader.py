#!/usr/bin/env python3
"""
smalldownloader - a tiny downloader with a terminal and a GUI.

THIS FILE IS GENERATED: it is the `smalldownloader/` package merged into one
standalone script by `tools/build_single_file.py`. Do not edit it by hand -
edit the package and rebuild (the test-suite fails if the two drift apart).

Nothing to install: save this file anywhere and run it.

    python smalldownloader.py https://example.com/big.iso -o ~/Downloads
    python smalldownloader.py gui            # open the window
    python smalldownloader.py --help
    python smalldownloader.py --dry-run URL  # size / name / range support

With no arguments it opens the window (double-click also works; rename it to
smalldownloader.pyw on Windows to hide the console). Only the Python standard
library is used, so Python 3.8+ is all you need - no pip, no admin rights.
"""

from __future__ import annotations

__version__ = '1.0.0'


# ==========================================================================
# curlimport.py
# ==========================================================================

"""Turn a browser 'Copy as cURL' snippet into request parameters.

Only the parts a downloader cares about are extracted: the URL, the headers,
the HTTP method and the POST body. Line continuations from cmd.exe (``^``),
bash (``\\``) and PowerShell (`` ``` ``) are all stripped first, so a snippet
copied on any platform can be pasted into either front-end.
"""


import re
from typing import Dict, Optional


class CurlRequest:
    """The pieces of a cURL command that matter for a download."""

    def __init__(
        self,
        url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        method: Optional[str] = None,
        body: Optional[str] = None,
    ) -> None:
        self.url = url
        self.headers = headers if headers is not None else {}
        self.method = method
        self.body = body

    @property
    def is_post(self) -> bool:
        return (self.method or "GET").upper() not in ("GET", "HEAD") or self.body is not None

    def as_post_data(self) -> Optional[bytes]:
        return self.body.encode("utf-8") if self.body is not None else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "CurlRequest(url=%r, headers=%d, method=%r, body=%s)" % (
            self.url, len(self.headers), self.method, "yes" if self.body else "no"
        )


def parse_curl(raw: str) -> CurlRequest:
    """Parse a pasted cURL command. Missing pieces simply come back as ``None``."""
    cleaned = re.sub(r"[\^\\`]\r?\n\s*", " ", raw or "")   # line continuations
    # cmd.exe escapes the quoting itself: -H ^"cookie: x^"  ->  -H "cookie: x"
    cleaned = re.sub(r"\^(?=[\"'])", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    url: Optional[str] = None
    match = re.search(r"curl(?:\.exe)?\s+(?:-[^\s]+\s+)*?['\"]?(https?://[^\s'\"]+)", cleaned,
                      re.IGNORECASE)
    if match:
        url = match.group(1).rstrip("^").strip("'\"")
    if not url:
        match = re.search(r"['\"]?(https?://[^\s'\"]+)", cleaned)
        if match:
            url = match.group(1).rstrip("^").strip("'\"")

    headers: Dict[str, str] = {}
    for header_match in re.finditer(r"(?:-H|--header)\s+(['\"])(.*?)\1", cleaned):
        line = header_match.group(2)
        if ":" in line:
            key, value = line.split(":", 1)
            if key.strip().lower() != "accept-encoding":
                headers[key.strip()] = value.strip()

    body: Optional[str] = None
    for data_match in re.finditer(r"(?:--data-raw|--data-binary|--data|-d)\s+(['\"])(.*?)\1",
                                  cleaned):
        body = data_match.group(2)
        headers.setdefault("Content-Type", "application/json;charset=UTF-8")
        break

    method: Optional[str] = None
    explicit = re.search(r"-X\s+([A-Za-z]+)", cleaned)
    if explicit:
        method = explicit.group(1).upper()
    elif body is not None:
        method = "POST"

    return CurlRequest(url=url, headers=headers, method=method, body=body)


# ==========================================================================
# core.py
# ==========================================================================

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


# ==========================================================================
# cli.py
# ==========================================================================

"""smalldownloader.cli - the terminal front-end.

A live, self-refreshing dashboard when attached to a real terminal, and plain
append-only lines when the output is piped, redirected or ``--quiet``.

    python -m smalldownloader https://example.com/a.zip https://example.com/b.zip
    python -m smalldownloader --file urls.txt -o ~/Downloads -n 8
    python -m smalldownloader --dry-run https://example.com/a.zip

Keys while it runs (interactive terminals only): ``p`` pause/resume, ``q`` quit.
Ctrl+C pauses; run the same command again to continue where it left off.
"""


import argparse
import os
import shutil
import signal
import sys
import time
from typing import Dict, List, Optional


# --------------------------------------------------------------------------- #
# tiny ANSI helpers
# --------------------------------------------------------------------------- #

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
CLEAR_LINE = "\x1b[2K"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
MOVE_UP = "\x1b[%dA"

STATE_COLORS = {
    PENDING: "\x1b[38;5;245m",
    RUNNING: "\x1b[36m",
    PAUSED: "\x1b[33m",
    DONE: "\x1b[32m",
    ERROR: "\x1b[31m",
    CANCELLED: "\x1b[38;5;245m",
}

STATE_LABELS = {
    PENDING: "queued",
    RUNNING: "running",
    PAUSED: "paused",
    DONE: "done",
    ERROR: "error",
    CANCELLED: "cancelled",
}


def _enable_ansi_on_windows() -> None:
    if os.name == "nt":
        try:
            os.system("")  # flips on VT processing in modern Windows consoles
        except Exception:
            pass


class Palette:
    """Colour + unicode support detection, with ``--no-color``/``--ascii`` opt-outs."""

    def __init__(self, color: bool = True, ascii_only: bool = False) -> None:
        self.color = color
        encoding = (getattr(sys.stdout, "encoding", None) or "utf-8").lower()
        self.unicode_ok = not ascii_only and "utf" in encoding
        if self.unicode_ok and os.name == "nt":
            try:  # a legacy code page would mangle the box characters
                "█".encode(sys.stdout.encoding or "utf-8")
            except Exception:
                self.unicode_ok = False

    def paint(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + RESET

    def mark(self, ok: bool) -> str:
        """``✔``/``✘`` when the console can show them, plain text otherwise."""
        if self.unicode_ok:
            return "✔" if ok else "✘"
        return "ok" if ok else "!!"

    def ellipsis(self) -> str:
        return "…" if self.unicode_ok else "~"

    def block(self, fraction: float, width: int) -> str:
        fraction = 0.0 if fraction < 0 else min(1.0, fraction)
        filled = int(round(fraction * width))
        if self.unicode_ok:
            return "█" * filled + "░" * (width - filled)
        return "#" * filled + "-" * (width - filled)


def progress_bar(state: Progress, width: int, palette: Palette) -> str:
    if state.state == DONE:
        fraction = 1.0
    elif state.total > 0:
        fraction = state.downloaded / float(state.total)
    else:
        fraction = 0.0
    if state.state == ERROR:
        return palette.paint("-" * width, STATE_COLORS[ERROR])
    color = STATE_COLORS.get(state.state, "")
    return palette.paint(palette.block(fraction, width), color)


def fit(text: str, width: int, ellipsis: str = "…") -> str:
    """Truncate *text* to *width* display columns (assumes 1 column per char)."""
    if width <= 1:
        return text[:width]
    if len(text) <= width:
        return text
    return text[: width - 1] + ellipsis


STATE_ORDER = {RUNNING: 0, PENDING: 1, PAUSED: 2, ERROR: 3, CANCELLED: 4, DONE: 5}


# --------------------------------------------------------------------------- #
# interactive keyboard (optional)
# --------------------------------------------------------------------------- #


def resolve_run_gui():
    """Return the GUI entry point, importing it only when it is needed.

    Written for both worlds: as a package it imports ``smalldownloader.gui``
    lazily (so Tkinter is not required for terminal use), and inside the
    generated single-file build every module shares one namespace, so the
    function is already in :func:`globals`.
    """
    if __package__:
        from importlib import import_module

        return import_module(".gui", __package__).run_gui
    return globals()["run_gui"]


class KeyReader:
    """Non-blocking single-key reads; silently does nothing when unsupported."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = bool(enabled) and sys.stdin.isatty()
        self._fd = None
        self._saved = None

    def __enter__(self) -> "KeyReader":
        if not self.enabled:
            return self
        try:
            if os.name == "nt":
                import importlib.util

                importlib.util.find_spec("msvcrt")  # availability probe only
            else:
                import termios
                import tty

                self._fd = sys.stdin.fileno()
                self._saved = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
        except Exception:
            self.enabled = False
        return self

    def __exit__(self, *exc_info) -> None:
        if self.enabled and self._saved is not None and self._fd is not None:
            try:
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                pass

    def get(self) -> Optional[str]:
        if not self.enabled:
            return None
        try:
            if os.name == "nt":
                import msvcrt

                if msvcrt.kbhit():
                    return msvcrt.getwch()
                return None
            import select

            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if ready:
                return sys.stdin.read(1)
        except Exception:
            self.enabled = False
        return None


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #


class Dashboard:
    """Renders the download table, redrawing in place on a real terminal."""

    def __init__(self, manager: DownloadManager, palette: Palette, live: bool,
                 show_keys: bool = True) -> None:
        self.manager = manager
        self.palette = palette
        self.live = live
        self.show_keys = show_keys
        self._drawn = 0
        self._last_status: Dict[int, str] = {}
        self._last_plain = 0.0

    # -- rendering --------------------------------------------------------- #

    def header(self) -> List[str]:
        colors = self.manager.counts()
        parts = [
            self.palette.paint("smalldownloader %s" % __version__, BOLD),
            "%d file(s)" % self.manager.task_count,
            "to %s" % self.manager.outdir,
            "%d connection(s) each" % self.manager.connections,
        ]
        line = "  ·  ".join(parts)
        status = "  ".join(
            "%s %d" % (STATE_LABELS[key], colors[key])
            for key in (DONE, ERROR, PAUSED, RUNNING, PENDING)
            if colors[key]
        )
        return [line, self.palette.paint(status, DIM)]

    def columns(self, width: int) -> tuple:
        bar_width = 14 if width < 90 else 20
        name_width = max(12, min(38, width - (bar_width + 46)))
        return name_width, bar_width

    def row(self, index: int, state: Progress, width: int) -> str:
        name_width, bar_width = self.columns(width)
        name = fit(state.name, name_width, self.palette.ellipsis()).ljust(name_width)
        bar = progress_bar(state, bar_width, self.palette)
        if state.total > 0:
            percent = "%3.0f%%" % state.percent
            amount = "%s/%s" % (human_bytes(state.downloaded), human_bytes(state.total))
        else:
            percent = "  ? "
            amount = human_bytes(state.downloaded)
        speed = ("%s/s" % human_bytes(state.speed)) if state.speed > 1 else "-"
        eta = human_time(state.eta) if state.eta else "-"
        label = STATE_LABELS.get(state.state, state.state)
        if state.state == ERROR and state.error:
            label = fit("error: %s" % state.error,
                        max(20, width - (name_width + bar_width + 30)),
                        self.palette.ellipsis())
        colour = STATE_COLORS.get(state.state, "")
        return "%2d %s %s %s  %-11s %-11s %-8s %s" % (
            index,
            name,
            bar,
            percent,
            amount,
            speed,
            eta,
            self.palette.paint(label, colour),
        )

    def render(self, final: bool = False) -> None:
        width = shutil.get_terminal_size((100, 24)).columns
        states = sorted(self.manager.progress(), key=lambda s: (STATE_ORDER.get(s.state, 9), s.id))
        lines = self.header() + [""] + [
            self.row(index, state, width) for index, state in enumerate(states, 1)
        ]
        total_speed = self.manager.total_speed
        if total_speed > 1:
            lines.append("")
            lines.append("total speed: %s/s" % human_bytes(total_speed))
        if final:
            lines.extend(self._final_lines(states))
        elif self.live and self.show_keys:
            lines.append(self.palette.paint("[p] pause/resume   [q] quit   Ctrl+C pause", DIM))
        self._draw(lines)

    def _final_lines(self, states: List[Progress]) -> List[str]:
        ok = [s for s in states if s.state == DONE]
        failed = [s for s in states if s.state == ERROR]
        paused = [s for s in states if s.state in (PAUSED, PENDING, RUNNING)]
        arrow = "→" if self.palette.unicode_ok else "->"
        lines = [""]
        if ok:
            lines.append(self.palette.paint(
                "%s %d file(s) downloaded" % (self.palette.mark(True), len(ok)),
                "\x1b[32m", BOLD))
            for state in ok:
                lines.append("   %s  %s  %s" % (state.name, arrow, state.dest or ""))
        if failed:
            lines.append(self.palette.paint(
                "%s %d file(s) failed" % (self.palette.mark(False), len(failed)),
                "\x1b[31m", BOLD))
            for state in failed:
                lines.append("   %s  %s  %s" % (state.name, arrow,
                                                state.error or "unknown error"))
        if paused:
            lines.append(self.palette.paint(
                "... %d file(s) not finished - run the same command again to resume"
                % len(paused), "\x1b[33m"))
        return lines

    def _draw(self, lines: List[str]) -> None:
        if not self.live:
            return
        out = sys.stdout
        if self._drawn:
            out.write(MOVE_UP % self._drawn)
        for line in lines:
            out.write(CLEAR_LINE + line + "\n")
        for _ in range(max(0, self._drawn - len(lines))):
            out.write(CLEAR_LINE + "\n")
        self._drawn = len(lines)
        out.flush()

    def finish(self) -> None:
        if self.live and self._drawn:
            sys.stdout.write(SHOW_CURSOR)
            sys.stdout.flush()
            self._drawn = 0

    # -- non-tty fallback -------------------------------------------------- #

    def log(self, message: str) -> None:
        if self.live:
            return
        print(message, flush=True)

    def maybe_log(self, states: List[Progress]) -> None:
        """When not live: print state changes always, progress every few seconds."""
        now = time.time()
        for state in states:
            previous = self._last_status.get(state.id)
            if previous != state.state:
                self._last_status[state.id] = state.state
                if previous is None and state.state == PENDING:
                    continue  # freshly queued: not interesting on its own
                if state.state == DONE:
                    self.log("done     %s -> %s" % (state.name, state.dest))
                elif state.state == ERROR:
                    self.log("failed   %s: %s" % (state.name, state.error))
                elif state.state == PAUSED:
                    self.log("paused   %s (%s/%s)" % (
                        state.name,
                        human_bytes(state.downloaded),
                        human_bytes(state.total) if state.total > 0 else "?",
                    ))
                elif state.state == RUNNING:
                    self.log("started  %s" % state.name)
                else:
                    self.log("%-8s %s" % (state.state, state.name))
        if now - self._last_plain >= 5.0:
            self._last_plain = now
            for state in states:
                if state.state == RUNNING and state.downloaded:
                    if state.total > 0:
                        self.log("         %s  %3.0f%%  %s/%s  %s/s  eta %s" % (
                            state.name,
                            state.percent,
                            human_bytes(state.downloaded),
                            human_bytes(state.total),
                            human_bytes(state.speed),
                            human_time(state.eta),
                        ))
                    else:
                        self.log("         %s  %s  %s/s" % (
                            state.name, human_bytes(state.downloaded), human_bytes(state.speed)
                        ))
            sys.stdout.flush()


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #


def parse_headers(pairs: Optional[List[str]]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for item in pairs or []:
        if ":" not in item:
            raise DownloadError("header must look like 'Name: value' (got %r)" % item)
        key, value = item.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smalldownloader",
        description="A tiny downloader: multi-connection, resumable, terminal + GUI.",
        epilog=(
            "examples:\n"
            "  smalldownloader https://example.com/big.iso -o ~/Downloads -n 8\n"
            "  smalldownloader --file urls.txt --jobs 3\n"
            "  smalldownloader --dry-run https://example.com/big.iso\n"
            "  smalldownloader gui\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("urls", nargs="*", help="one or more URLs (http/https)")
    parser.add_argument("-V", "--version", action="version",
                        version="smalldownloader %s" % __version__)
    parser.add_argument("-o", "--output", metavar="DIR", default=None,
                        help="directory to save into (default: ~/Downloads)")
    parser.add_argument("-f", "--file", metavar="FILE", default=None,
                        help="read URLs from a text file, one per line ('#' comments)")
    parser.add_argument("-O", "--outfile", metavar="NAME", default=None,
                        help="save as NAME (only useful with a single URL)")
    parser.add_argument("-n", "--connections", type=int, default=4, metavar="N",
                        help="parallel connections per file (default: 4, 1 disables splitting)")
    parser.add_argument("-j", "--jobs", type=int, default=2, metavar="N",
                        help="files downloaded at the same time (default: 2)")
    parser.add_argument("-r", "--retries", type=int, default=3, metavar="N",
                        help="retries per connection (default: 3)")
    parser.add_argument("-b", "--cookie", metavar="TEXT|FILE", default=None,
                        help="raw Cookie header, or a Netscape cookies.txt path")
    parser.add_argument("-H", "--header", action="append", metavar="'Name: value'",
                        help="extra request header (repeatable)")
    parser.add_argument("-A", "--user-agent", default=DEFAULT_USER_AGENT, metavar="UA")
    parser.add_argument("-t", "--timeout", type=float, default=30.0, metavar="SECONDS")
    parser.add_argument("--no-resume", action="store_true", help="do not resume partial files")
    parser.add_argument("--no-overwrite", action="store_true",
                        help="keep existing files (save as 'name (1).ext' instead)")
    parser.add_argument("-k", "--insecure", action="store_true",
                        help="skip TLS certificate verification")
    parser.add_argument("--dry-run", action="store_true",
                        help="only query the servers: show size, name and range support")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="no live dashboard, no colours (good for scripts/logs)")
    parser.add_argument("--ascii", action="store_true", help="plain ASCII output")
    return parser


def read_url_file(path: str) -> List[str]:
    urls = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    except OSError as exc:
        raise DownloadError("cannot read URL file %s: %s" % (path, exc)) from exc
    return urls


def cookie_header(value: str) -> str:
    """Accept either a raw cookie string or a Netscape ``cookies.txt`` file."""
    if not os.path.isfile(value):
        return value
    jar: List[str] = []
    try:
        with open(value, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                fields = line.split("\t")
                if len(fields) >= 7:
                    jar.append("%s=%s" % (fields[5], fields[6]))
    except OSError as exc:
        raise DownloadError("cannot read cookie file %s: %s" % (value, exc)) from exc
    return "; ".join(jar)


# --------------------------------------------------------------------------- #
# dry run
# --------------------------------------------------------------------------- #


def run_dry(urls: List[str], args, headers: Dict[str, str], palette: Palette) -> int:
    failures = 0
    for url in urls:
        try:
            info = probe(
                url,
                headers=headers,
                user_agent=args.user_agent,
                timeout=args.timeout,
                insecure=args.insecure,
            )
        except DownloadError as exc:
            failures += 1
            print(palette.paint("%s %s" % (palette.mark(False), url), "\x1b[31m"))
            print("   %s" % exc)
            continue
        print(palette.paint("%s %s" % (palette.mark(True), url), "\x1b[32m"))
        print("   name        : %s" % (info.filename or "(from URL)"))
        print("   size        : %s" % (human_bytes(info.size) if info.size >= 0 else "unknown"))
        print("   segments    : %s" % ("yes (Range supported)" if info.accept_ranges else "no"))
        print("   content-type: %s" % (info.content_type or "?"))
        if info.final_url != url:
            print("   redirects to: %s" % info.final_url)
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def build_gui_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smalldownloader gui",
        description="Open the smalldownloader window (Tkinter).",
    )
    parser.add_argument("urls", nargs="*", help="optional URLs to queue on startup")
    parser.add_argument("-o", "--output", metavar="DIR", default=None,
                        help="initial download folder (default: ~/Downloads)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _enable_ansi_on_windows()
    raw = list(sys.argv[1:] if argv is None else argv)

    if raw and raw[0].lower() in ("gui", "window", "app"):
        gui_args = build_gui_parser().parse_args(raw[1:])
        try:
            run_gui = resolve_run_gui()
        except ImportError as exc:
            print("smalldownloader: the GUI needs Tkinter (%s)." % exc, file=sys.stderr)
            print("  Debian/Ubuntu : sudo apt install python3-tk", file=sys.stderr)
            print("  Fedora        : sudo dnf install python3-tkinter", file=sys.stderr)
            print("  Windows/macOS : use the python.org build of Python", file=sys.stderr)
            print("Use the terminal mode meanwhile: smalldownloader <URL>", file=sys.stderr)
            return 1

        return run_gui(urls=gui_args.urls, outdir=gui_args.output)

    args = build_parser().parse_args(raw)

    try:
        headers = parse_headers(args.header)
    except DownloadError as exc:
        print("smalldownloader: %s" % exc, file=sys.stderr)
        return 2

    urls: List[str] = list(args.urls)
    if args.file:
        try:
            urls.extend(read_url_file(args.file))
        except DownloadError as exc:
            print("smalldownloader: %s" % exc, file=sys.stderr)
            return 2
    if not urls and not sys.stdin.isatty():
        urls = [line.strip() for line in sys.stdin if line.strip()]
    if not urls:
        build_parser().print_help()
        print("\nNo URLs given. Try `smalldownloader gui` for the windowed version.")
        return 2

    if args.cookie:
        try:
            headers.setdefault("Cookie", cookie_header(args.cookie))
        except DownloadError as exc:
            print("smalldownloader: %s" % exc, file=sys.stderr)
            return 2

    if args.outfile and len(urls) > 1:
        print("smalldownloader: --outfile ignored (more than one URL given)", file=sys.stderr)
        args.outfile = None

    ascii_only = args.ascii or args.quiet
    live = sys.stdout.isatty() and not args.quiet
    palette = Palette(color=live and not args.quiet, ascii_only=ascii_only)

    if args.dry_run:
        return run_dry(urls, args, headers, palette)

    try:
        manager = DownloadManager(
            outdir=args.output,
            connections=args.connections,
            jobs=args.jobs,
            retries=args.retries,
            headers=headers,
            user_agent=args.user_agent,
            resume=not args.no_resume,
            overwrite=not args.no_overwrite,
            insecure=args.insecure,
            timeout=args.timeout,
        )
        for url in urls:
            manager.add(url, filename=args.outfile)
    except (DownloadError, OSError) as exc:
        print("smalldownloader: %s" % exc, file=sys.stderr)
        return 2
    except ValueError as exc:
        print("smalldownloader: %s" % exc, file=sys.stderr)
        return 2

    dashboard = Dashboard(manager, palette, live)
    interrupted = {"flag": False, "asked_quit": False}
    original_sigint = None

    def on_sigint(signum, frame):  # noqa: ARG001
        if interrupted["flag"]:  # second Ctrl+C: leave immediately
            dashboard.finish()
            sys.stdout.write("\n")
            os._exit(130)
        interrupted["flag"] = True

    try:
        original_sigint = signal.signal(signal.SIGINT, on_sigint)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, on_sigint)
    except (ValueError, OSError):  # not on the main thread (embedded use)
        original_sigint = None

    if live:
        sys.stdout.write(CLEAR_LINE)
        sys.stdout.write(HIDE_CURSOR)
        sys.stdout.flush()

    manager.start()
    exit_code = 0
    try:
        with KeyReader(enabled=live) as keys:
            while True:
                states = manager.progress()
                if live:
                    dashboard.render()
                else:
                    dashboard.maybe_log(states)
                if not manager.is_running():
                    waiting = any(state.state in (PAUSED, PENDING, RUNNING) for state in states)
                    if not (live and waiting and not interrupted["flag"]):
                        break
                if keys.enabled:
                    key = keys.get()
                    if key in ("q", "Q", "\x03"):
                        manager.stop(pause=True, wait=False)
                        interrupted["asked_quit"] = True
                        break
                    if key in ("p", "P"):
                        if any(state.state in (RUNNING, PENDING) for state in states):
                            manager.pause_all()
                        else:
                            manager.resume_all()
                if interrupted["flag"]:
                    manager.stop(pause=True, wait=False)
                    break
                time.sleep(0.25)
    finally:
        if manager.is_running():
            manager.stop(pause=True, wait=True)
        if original_sigint is not None:
            try:
                signal.signal(signal.SIGINT, original_sigint)
                if hasattr(signal, "SIGTERM"):
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
            except (ValueError, OSError):
                pass
        dashboard.render(final=True)
        dashboard.finish()

    counts = manager.counts()
    unfinished = counts[PAUSED] + counts[PENDING] + counts[RUNNING] + counts[CANCELLED]
    if counts[ERROR]:
        exit_code = 1
    elif unfinished:
        exit_code = 130
    if not live:
        summary = "%d done, %d failed, %d paused/cancelled" % (
            counts[DONE], counts[ERROR], counts[PAUSED] + counts[PENDING] + counts[CANCELLED]
        )
        print("smalldownloader: %s" % summary)
    return exit_code


# ==========================================================================
# gui.py
# ==========================================================================

"""smalldownloader.gui - the windowed front-end (Tkinter, no extra installs).

    python -m smalldownloader gui
    python -m smalldownloader gui -o ~/Downloads https://example.com/big.iso

Tkinter ships with the python.org installers on Windows/macOS and is a distro
package (``python3-tk``) on Linux, so the window stays dependency-free. The GUI
is only a view: every download runs in :class:`smalldownloader.core.DownloadManager`
and the window polls it a few times per second.
"""


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

GUI_STATE_COLORS = {
    PENDING: MUTED,
    RUNNING: BLUE,
    PAUSED: YELLOW,
    DONE: GREEN,
    ERROR: RED,
    CANCELLED: MUTED,
}

GUI_STATE_LABELS = {
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
            for state, color in GUI_STATE_COLORS.items():
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
            label = GUI_STATE_LABELS.get(state.state, state.state)
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
                    GUI_STATE_LABELS.get(state.state, state.state),
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
def _standalone_main(argv=None) -> int:
    """No arguments -> open the window; otherwise behave exactly like the CLI."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        try:
            if run_gui() == 0:
                return 0
        except (KeyboardInterrupt, EOFError):
            return 130
    return main(args)


if __name__ == "__main__":
    sys.exit(_standalone_main())
