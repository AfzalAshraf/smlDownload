"""smalldownloader - a tiny, dependency-free downloader with a terminal and a GUI.

The engine lives in :mod:`smalldownloader.core` and is shared by both front-ends:

    >>> from smalldownloader import DownloadManager
    >>> manager = DownloadManager("/tmp/downloads", connections=4)
    >>> task = manager.add("https://example.com/file.zip")
    >>> manager.start(block=True)          # doctest: +SKIP
    >>> task.state                          # doctest: +SKIP
    'done'

Command line (terminal UI)::

    python -m smalldownloader https://example.com/file.zip -o ~/Downloads
    python -m smalldownloader gui

Nothing here imports third-party packages: standard library only.
"""

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
    DownloadTask,
    ProbeInfo,
    Progress,
    human_bytes,
    human_time,
    parse_content_disposition,
    probe,
    safe_filename,
)

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "DownloadManager",
    "DownloadTask",
    "DownloadError",
    "Progress",
    "ProbeInfo",
    "probe",
    "safe_filename",
    "parse_content_disposition",
    "human_bytes",
    "human_time",
    "PENDING",
    "RUNNING",
    "PAUSED",
    "DONE",
    "ERROR",
    "CANCELLED",
    "DEFAULT_USER_AGENT",
]
