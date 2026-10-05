#!/usr/bin/env python
"""Double-click launcher for the smalldownloader window.

On Windows, ``.pyw`` files start with pythonw.exe (no console window), so this
file opens the GUI directly. On Linux/macOS run it as a normal script:

    python smalldownloader.pyw            # GUI
    python smalldownloader.pyw <URL>      # GUI with a URL already queued

The terminal version is ``python -m smalldownloader <URL>``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smalldownloader.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["gui"] + sys.argv[1:]))
