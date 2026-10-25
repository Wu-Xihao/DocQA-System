"""Repository launcher: python scripts/docqa.py doc2md --help (no installation needed)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from docqa.cli.main import app  # noqa: E402

if __name__ == "__main__":
    app()
