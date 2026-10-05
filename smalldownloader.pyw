#!/usr/bin/env python
"""Double-click launcher for smalldownloader (Windows: no console window).

Everything lives in ``smalldownloader.py`` next to this file - this script only
starts it in window mode, so there is exactly one copy of the downloader:

    double-click            -> opens the window (pythonw.exe, no console)
    smalldownloader.pyw URL -> opens the window with that URL already queued
    python smalldownloader.pyw URL -o ~/Downloads   -> same, with options

If ``smalldownloader.py`` is missing (for example when only the package folder
was copied), it falls back to importing ``smalldownloader.gui`` from the package.
"""

import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.join(HERE, "smalldownloader.py")


def main() -> int:
    if os.path.exists(BUNDLE):
        sys.argv = [BUNDLE, "gui"] + sys.argv[1:]
        runpy.run_path(BUNDLE, run_name="__main__")
        return 0

    sys.path.insert(0, HERE)
    try:
        from smalldownloader.gui import run_gui
    except ImportError as exc:
        sys.stderr.write(
            "smalldownloader: neither smalldownloader.py nor the package are "
            "next to this file (%s)\n" % exc
        )
        return 1
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
