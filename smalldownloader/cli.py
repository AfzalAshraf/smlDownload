"""smalldownloader.cli - the terminal front-end.

A live, self-refreshing dashboard when attached to a real terminal, and plain
append-only lines when the output is piped, redirected or ``--quiet``.

    python -m smalldownloader https://example.com/a.zip https://example.com/b.zip
    python -m smalldownloader --file urls.txt -o ~/Downloads -n 8
    python -m smalldownloader --dry-run https://example.com/a.zip

Keys while it runs (interactive terminals only): ``p`` pause/resume, ``q`` quit.
Ctrl+C pauses; run the same command again to continue where it left off.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
import time
from typing import Dict, List, Optional

from . import __version__
from .core import (
    CANCELLED,
    DEFAULT_USER_AGENT,
    DONE,
    ERROR,
    PAUSED,
    PENDING,
    RUNNING,
    DownloadError,
    DownloadManager,
    Progress,
    human_bytes,
    human_time,
    probe,
)

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
            from .gui import run_gui
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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
