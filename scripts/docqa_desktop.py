"""Entry script for PyInstaller and source-mode launcher checks."""

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from docqa.desktop import entry  # noqa: E402

if __name__ == "__main__":
    entry()
