# smalldownloader

A tiny downloader with **a terminal and a GUI**.

Splits a file across several connections, resumes after a Ctrl+C (or a crash),
retries dropped connections, and needs **nothing but the Python standard library** -
no `pip install`, no `aria2c` binary, no 20 MB dependency tree.

```
python smalldownloader.py https://example.com/big.iso -o ~/Downloads -n 8
python smalldownloader.py                          # opens the window
```

---

## No install: one file, any machine

**[`smalldownloader.py`](smalldownloader.py) is the whole program in one file** -
~2.8k lines, no package folder, no `pip`, no build step. Download that single file
and run it:

```bash
# 1. get the file (download it from GitHub, or copy it over on a USB stick)
curl -O https://raw.githubusercontent.com/AfzalAshraf/smlDownload/main/smalldownloader.py

# 2. run it - nothing else to do
python smalldownloader.py https://example.com/big.iso -o ~/Downloads -n 8
python smalldownloader.py                          # window
python smalldownloader.py --dry-run https://example.com/big.iso
```

Requirements: **Python 3.8 or newer, and that is all.** No admin rights, no
virtualenv, no internet access at install time. It works exactly the same on
Windows, Linux and macOS, and the same file can be renamed to
`smalldownloader.pyw` so a double-click opens the window without a console.

If the file has no arguments it opens the window; if Tkinter is missing it prints
how to get it and keeps working in the terminal. The package below is the source
of truth - the single file is generated from it with
`python tools/build_single_file.py`, and the test-suite fails if someone forgets
to regenerate, so the two can never drift apart.

---

## Why it is small

| | |
|---|---|
| Dependencies | **none** - stdlib only (`urllib`, `socket`, `threading`, `tkinter` for the window) |
| Code | 6 files, ~2.4k lines of plain Python, one engine shared by both front-ends |
| Setup | copy the folder and run it (or `pip install .` for a `smalldownloader` command) |
| Platforms | Windows, Linux, macOS (Python 3.8+) |

The old `aria2.pyw` in this repository is untouched - `smalldownloader` is a
separate, self-contained app that does not shell out to `aria2c`.

## Features

* **Multi-connection** - the file is split into ranges and fetched in parallel
  (`-n`, default 4). Segments never overlap and are written straight into place.
* **Resumable** - partial data lives in `<name>.smlpart`, progress in
  `<name>.smlpart.json`. Re-run the same URL and it continues where it stopped,
  even in a new process after a reboot. `--no-resume` disables it.
* **Automatic fallback** - servers that ignore `Range`, gzip the body, or never
  send `Content-Length` (chunked) still download fine, just without splitting.
* **Retries that understand progress** - a flaky host that drops the connection
  every few hundred KB still completes; a host that hands out nothing fails fast.
* **Queue** - several files at once (`-j`, default 2), pause/resume/cancel,
  retry failed downloads.
* **Terminal dashboard** - live bars, speed, ETA, totals. Falls back to plain
  append-only lines when piped, and never emits Unicode/ANSI on request
  (`--ascii`, `-q`).
* **GUI** - Tkinter window with a sortable queue table, activity log, per-download
  details, right-click actions (open file, show in folder, copy URL, retry) and a
  **"Import from browser cURL"** dialog that reuses cookies/headers/POST bodies
  for sessions that need a login.
* **Sensible HTTP** - `Content-Disposition` filenames (incl. `filename*=UTF-8''…`),
  redirects, cookies, custom headers, custom User-Agent, `--insecure` for broken
  certs, Windows-safe filenames, `name (1).ext` instead of silently overwriting.

## Terminal

```
$ smalldownloader https://example.com/big.iso -o ~/Downloads -n 8
```

```
smalldownloader 1.0.0  ·  3 file(s)  ·  to /home/me/Downloads  ·  8 connection(s) each
done 1  running 1  queued 1

 1 ubuntu-24.04-desktop-amd64.iso         ████████████░░░░░░░░  61%  3.7 GB/6.1 GB 11.4 MB/s   03:26    running
 2 annual-report-2025.pdf                 ████████████████████ 100%  4.2 MB/4.2 MB -           -        done
 3 dataset.tar.gz                         ░░░░░░░░░░░░░░░░░░░░   ?   0 B         -           -        queued

total speed: 11.4 MB/s
[p] pause/resume   [q] quit   Ctrl+C pause
```

### Keys, exit codes, resumed runs

* `p` pause/resume the whole queue, `q` quit (keeps partial files), `Ctrl+C` pause.
  A second `Ctrl+C` leaves immediately.
* Exit codes: `0` everything downloaded, `1` at least one failure, `2` bad usage,
  `130` interrupted with work left over.
* While a download is paused, the `.smlpart` payload **and** the `.smlpart.json`
  progress file stay on disk. Running the same command again continues from there
  and renames the file into place only when it is complete.

When the output is not a terminal (pipe, file, CI) the dashboard is replaced by
plain lines - ideal for logs and scripts:

```
$ smalldownloader -f urls.txt -o ~/Downloads -n 6 -j 3
started  ubuntu-mini.iso
done     naïve résumé.pdf -> /home/me/Downloads/naïve résumé.pdf
done     ubuntu-mini.iso -> /home/me/Downloads/ubuntu-mini.iso
failed   data.bin: HTTP 404 Not Found
smalldownloader: 2 done, 1 failed, 0 paused/cancelled
```

### Look before you download

```
$ smalldownloader --dry-run --file urls.txt
✔ https://releases.example.com/ubuntu-24.04.1-desktop-amd64.iso
   name        : ubuntu-24.04.1-desktop-amd64.iso
   size        : 6.1 GB
   segments    : yes (Range supported)
   content-type: application/x-iso9660-image
```

`--dry-run` only sends a `HEAD` / one-byte `Range` request, so you can check the
size, the real filename and whether splitting is supported before committing
bandwidth. `-f/--file` reads URLs from a text file (`#` comments allowed);
URLs can also be piped in on stdin.

### Options

| Option | Meaning |
|---|---|
| `-o, --output DIR` | where to save (default `~/Downloads`) |
| `-f, --file FILE` | read URLs from a file, one per line |
| `-O, --outfile NAME` | force the file name (single URL) |
| `-n, --connections N` | connections per file, `1` disables splitting (default 4) |
| `-j, --jobs N` | files downloaded simultaneously (default 2) |
| `-r, --retries N` | retries per connection (default 3) |
| `-b, --cookie TEXT\|FILE` | raw cookie header or a Netscape `cookies.txt` |
| `-H, --header 'Name: value'` | extra request header (repeatable) |
| `-A, --user-agent UA` | override the User-Agent |
| `-t, --timeout SECONDS` | socket timeout (default 30) |
| `--no-resume` | ignore leftover `.smlpart` data |
| `--no-overwrite` | keep existing files, save as `name (1).ext` |
| `-k, --insecure` | skip TLS certificate verification |
| `--dry-run` | probe only, download nothing |
| `-q, --quiet` | no dashboard and no colours |
| `--ascii` | plain ASCII output for legacy consoles |

## GUI

```
python -m smalldownloader gui          # or double-click smalldownloader.pyw
python -m smalldownloader gui -o ~/Downloads https://example.com/big.iso
```

Layout sketch (the real window is tidier than ASCII can show):

```
┌ smalldownloader ─────────────────────────────────────── 2 running · 41.2 MB/s ┐
│ Save to [ ~/Downloads               ] [Browse…]  Connections [8]  Files [2]    │
├────────────────────────────────────────────────────────────────────────────────┤
│ [ paste links here, one per line… ]                    [Add to queue][cURL…]   │
│ [Start] [Pause] [Resume] [Cancel]        [Retry failed] [Remove finished]      │
├────────────────────────────────────────────────────────────────────────────────┤
│ #  File                        Progress              Size        Speed  Status │
│ 1  ubuntu-24.04.iso            ████████████░░░░ 61%   3.7/6.1 GB  11.4 MB/s ⋯ │
│ 2  report.pdf                  ████████████████ 100%  4.2/4.2 MB  -       done │
│ 3  dataset.tar.gz              ░░░░░░░░░░░░░░░░   ?    0 B         -     queued│
├────────────────────────────────────────────────────────────────────────────────┤
│ Activity | Details        ✔ ubuntu-mini.iso → ~/Downloads/ubuntu-mini.iso      │
└────────────────────────────────────────────────────────────────────────────────┘
```

Everything the window does goes through the same `DownloadManager` as the terminal,
so behaviour (segmentation, resume, retries, file naming) is identical. The table
updates 3× per second; use the right-click menu for per-download actions, and
"Import from browser cURL" to paste DevTools' *Copy as cURL* when a URL needs a
session (headers, cookies and POST bodies are reused; `^`, `\` and backtick line
continuations are all understood, so snippets copied on Windows work too).

Tkinter is part of the python.org builds on Windows/macOS. On Linux install it
once:

```bash
sudo apt install python3-tk        # Debian/Ubuntu
sudo dnf install python3-tkinter   # Fedora
```

The terminal mode has no such requirement.

## Install

**Usually: don't.** Copy [`smalldownloader.py`](smalldownloader.py) anywhere and run
it (see above). The rest of this section is for the repository checkout and for
people who prefer a command on their `PATH`.

From the checkout:

```bash
python -m smalldownloader --help
```

As a command, if you want one:

```bash
pip install .          # adds the `smalldownloader` command
pipx install .         # isolated, if you prefer
```

On Windows, `smalldownloader.pyw` starts the GUI without a console window: it
loads `smalldownloader.py` when it sits next to it, and otherwise falls back to the
package.

## Use it as a library

```python
from smalldownloader import DownloadManager

manager = DownloadManager("~/Downloads", connections=8, jobs=2)
manager.add("https://example.com/a.iso")
manager.add("https://example.com/b.iso", headers={"Cookie": "session=abc"})
manager.start(block=True)

for task in manager.tasks:
    print(task.state, task.final_path, task.error)   # done /path/a.iso None

# live snapshots for your own UI (bytes, speed, ETA, percent)
for state in manager.progress():
    print(state.name, state.percent, state.speed)
```

Pause/resume/cancel from another thread:

```python
manager.pause_all(); manager.resume_all(); manager.cancel_all()
manager.stop(pause=True)        # stop the scheduler, keep partial files
```

## Tests

71 tests, no network access required - they run against a local HTTP server that
pretends to be a hostile host (no range support, chunked bodies, dropped
connections, redirects, `Content-Disposition` oddities, 403/404, faked
multi-gigabyte sizes):

```bash
python -m unittest discover -s tests -t .        # ~40 s
python tests/local_server.py                     # poke the fake host manually
```

Covered: helpers and header/filename parsing, probing, single- and
multi-connection downloads against every server flavour, byte-exact content
checks, pause → resume → identical file, cancellation, retries (including
progress-based retries), queue behaviour, the CLI (all exit codes, dry-run, URL
files, cookies, quiet mode), the live ANSI dashboard inside a pty including the
`q` key, and the GUI logic (window construction, table rendering, transitions,
cURL import) against a fake Tk.

The single-file edition has its own tests: it must be up to date with the
package, contain no package imports, run with an empty `PYTHONPATH` from a
throwaway folder, download byte-identical data, survive SIGTERM and resume, and
open the window when started with no arguments (using a stub Tkinter).

The engine was also checked against a real CDN: an 11.1 MB wheel downloaded from
`files.pythonhosted.org` over 8 connections matched the sha256 published by PyPI.

## Layout

```
smalldownloader.py    the single-file edition: copy this anywhere and run it
smalldownloader/
├── __init__.py       public API re-exports
├── core.py           engine: probing, ranges, segments, retries, resume, queue
├── cli.py            terminal front-end (dashboard, keys, exit codes)
├── curlimport.py     "Copy as cURL" parser
├── gui.py            Tkinter front-end
└── __main__.py       python -m smalldownloader
smalldownloader.pyw   double-click launcher for the GUI (Windows)
tools/                build_single_file.py (package -> single file)
tests/                local fake HTTP server + unittest suite
aria2.pyw             the original standalone aria2 GUI script (unchanged)
```

## Notes and limits

* A ranged download is only split when the server advertises `Range` **and** a
  length; everything else is streamed in a single connection (still resumable
  when the server supports ranges).
* POST/PUT downloads are never split - ranged bodies are not something we trust.
  POST sessions imported from cURL are sent as one stream.
* The `Accept-Encoding: identity` request header is sent deliberately: byte
  offsets are only meaningful on an unmodified body.
* Partial files (`.smlpart`, `.smlpart.json`) are deleted on success. If a
  download is interrupted, they are exactly what makes the resume work - delete
  them to start over.
* Verified with Python 3.11 on Linux; the code paths are written for Windows and
  macOS too (`CREATE_NO_WINDOW`-free, no shell invocation, no POSIX-only APIs in
  the engine), but those two were not run here.
