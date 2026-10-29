"""Read-only dependency/import check; does not install packages or call models."""

from __future__ import annotations

import argparse
import importlib
import platform
import shutil
import sqlite3
import struct
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMPORT_NAMES = {
    "pydantic": "pydantic",
    "pydantic-settings": "pydantic_settings",
    "PyYAML": "yaml",
    "python-dotenv": "dotenv",
    "packaging": "packaging",
    "httpx": "httpx",
    "pdfplumber": "pdfplumber",
    "pypdfium2": "pypdfium2",
    "python-docx": "docx",
    "python-pptx": "pptx",
    "Pillow": "PIL",
    "numpy": "numpy",
    "jieba": "jieba",
    "typer": "typer",
    "rich": "rich",
    "portalocker": "portalocker",
    "pytest": "pytest",
    "pytest-asyncio": "pytest_asyncio",
    "pytest-cov": "pytest_cov",
    "respx": "respx",
    "ruff": "ruff",
    "fastapi": "fastapi",
    "starlette": "starlette",
    "uvicorn": "uvicorn",
    "sqlite-vec": "sqlite_vec",
    "playwright": "playwright",
}


def requirement_lines(path: Path, seen: set[Path]) -> list[str]:
    path = path.resolve()
    if path in seen:
        return []
    seen.add(path)
    result: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            result.extend(requirement_lines(path.parent / line[3:].strip(), seen))
        else:
            result.append(line)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Accepted for compatibility; the consolidated manifest checks all three groups.
    parser.add_argument(
        "--dev", action="store_true", help="Compatibility flag: all tools are checked"
    )
    parser.add_argument(
        "--web", action="store_true", help="Compatibility flag: all tools are checked"
    )
    parser.add_argument(
        "--service", action="store_true", help="Compatibility flag: all tools are checked"
    )
    parser.add_argument(
        "--system",
        action="store_true",
        help="Also check Tesseract and LibreOffice on PATH (not language packs)",
    )
    args = parser.parse_args()
    print(f"Python: {platform.python_version()}")
    print(f"Executable: {sys.executable}")
    print(f"Prefix: {sys.prefix}")
    bits = struct.calcsize("P") * 8
    print(f"Platform: {platform.system()} {platform.machine()} / {bits}-bit")
    errors: list[str] = []
    if sys.version_info[:2] != (3, 12):
        errors.append("Project baseline requires Python 3.12.x")
    if bits != 64:
        errors.append("Project baseline requires a 64-bit Python interpreter")

    try:
        from packaging.requirements import Requirement
    except ImportError:
        print("[FAIL] packaging is missing. Install requirements.txt first.")
        return 1

    lines: list[str] = []
    seen: set[Path] = set()
    lines.extend(requirement_lines(ROOT / "requirements.txt", seen))

    for line in lines:
        req = Requirement(line)
        if req.marker is not None and not req.marker.evaluate():
            continue
        try:
            installed = metadata.version(req.name)
            if installed not in req.specifier:
                raise RuntimeError(f"{installed} does not satisfy {req.specifier}")
            importlib.import_module(IMPORT_NAMES[req.name])
            print(f"[OK] {req.name} {installed}")
        except Exception as exc:
            message = f"{req.name}: {type(exc).__name__}: {exc}"
            errors.append(message)
            print(f"[FAIL] {message}")

    try:
        with sqlite3.connect(":memory:") as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        print(f"[OK] SQLite {sqlite3.sqlite_version} (Python standard library)")
    except Exception as exc:
        errors.append(f"SQLite: {exc}")

    if args.system:
        for label, candidates in (
            ("Tesseract", ("tesseract",)),
            ("LibreOffice", ("soffice.com", "soffice")),
        ):
            location = next((p for name in candidates if (p := shutil.which(name))), None)
            if location is None:
                message = f"{label} executable not found on PATH"
                errors.append(message)
                print(f"[FAIL] {message}")
            else:
                print(f"[OK] {label}: {location} (PATH check only)")

    if errors:
        print(f"\nCHECK FAILED: {len(errors)} item(s). No environment changes made.")
        return 1
    print("\nDEPENDENCY CHECK PASSED.")
    print("Next: run python -m pip check; model endpoints and document parsing remain unverified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
