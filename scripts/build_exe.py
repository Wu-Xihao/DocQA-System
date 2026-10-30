"""Build a shareable bundle without copying local secrets or business data."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from importlib.metadata import distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32:
        raise SystemExit("Use the project's Windows x64 Python 3.12 environment")
    previous = ROOT / "dist/DocQA"
    if previous.exists() and (
        (previous / "runtime").exists() or (previous / "data").exists()
        or (previous / "configs/.env").exists()
        or any((previous / "configs").glob("local.*"))
    ):
        raise SystemExit("dist/DocQA contains user data. Move/back up that folder before rebuilding.")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join([
        str(ROOT / "build/packaging-tools"), str(ROOT / "src"),
    ])
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build/exe"),
        str(ROOT / "packaging/DocQA.spec"),
    ], cwd=ROOT, env=environment, check=True)
    bundle = ROOT / "dist/DocQA"
    configs = bundle / "configs"
    configs.mkdir(exist_ok=True)
    for path in sorted((ROOT / "configs").glob("*.example.yaml")):
        shutil.copyfile(path, configs / path.name)
    shutil.copyfile(ROOT / "configs/.env.example", configs / ".env.example")
    for name in ("README.md", "AGENTS.md", "AGENT.md"):
        shutil.copyfile(ROOT / name, bundle / name)
    shutil.copyfile(ROOT / "packaging/使用说明.txt", bundle / "使用说明.txt")
    (bundle / "Document").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "Document/系统设计说明.md", bundle / "Document/系统设计说明.md")
    licenses = bundle / "licenses"
    licenses.mkdir(exist_ok=True)
    python_license = Path(sys.base_prefix) / "LICENSE_PYTHON.txt"
    if python_license.is_file():
        shutil.copyfile(python_license, licenses / "LICENSE_PYTHON.txt")
    for distribution in distributions():
        name = distribution.metadata.get("Name", "unknown")
        for item in distribution.files or []:
            relative = Path(str(item))
            if ".dist-info" not in str(item) or not any(
                token in relative.name.lower() for token in ("license", "copying", "notice")
            ):
                continue
            destination = licenses / name / relative.name
            if destination.exists():
                continue
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(distribution.locate_file(item), destination)
    packages = {d.metadata["Name"]: d.version for d in distributions() if d.metadata.get("Name")}
    (bundle / "build-info.json").write_text(json.dumps({
        "python": sys.version.split()[0], "platform": sys.platform,
        "build_environment_packages": packages,
        "private_data_included": False, "config_policy": "examples-only",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    subprocess.run([str(bundle / "DocQA.exe"), "--check"], check=True, timeout=60)
    forbidden = [p for p in bundle.rglob("*") if p.is_file() and (
        p.name == ".env" or p.name.startswith("local.") or p.suffix in {".sqlite", ".sqlite3"}
    )]
    if forbidden:
        raise SystemExit("Release contains local configuration or runtime data")
    manifest = {
        p.relative_to(bundle).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(bundle.rglob("*")) if p.is_file()
    }
    (bundle / "checksums.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive = ROOT / "dist/DocQA-Windows-x64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(bundle.parent))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(digest + "\n", encoding="ascii")
    print(f"Built: {archive}\nSHA256: {digest}")


if __name__ == "__main__":
    main()
