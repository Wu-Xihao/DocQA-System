# Sources are included for existing parser/index implementation fingerprints.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
datas = collect_data_files("docqa", include_py_files=True)
for package in ("jieba", "sqlite_vec", "pypdfium2", "pypdfium2_raw", "docx", "pptx"):
    datas += collect_data_files(package)
for package in (
    "jieba", "pydantic", "pdfplumber", "pypdfium2", "python-docx", "python-pptx",
    "Pillow", "numpy", "sqlite-vec", "openpyxl", "fastapi", "starlette", "uvicorn",
):
    datas += copy_metadata(package)
for path in sorted((root / "configs").glob("*.example.yaml")):
    datas.append((str(path), "configs"))
datas.append((str(root / "configs/.env.example"), "configs"))
for name in ("README.md", "AGENTS.md", "AGENT.md"):
    datas.append((str(root / name), "docs"))
datas.append((str(root / "Document/系统设计说明.md"), "docs"))
a = Analysis(
    [str(root / "packaging/entrypoint.py")], pathex=[str(root / "src")], datas=datas,
    hiddenimports=collect_submodules("docqa") + collect_submodules("uvicorn"),
    excludes=["torch", "tensorflow", "FlagEmbedding", "transformers", "scipy", "pandas",
              "matplotlib", "IPython", "pytest", "playwright", "tkinter", "PyQt5", "PyQt6"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="DocQA", console=True,
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          version=str(root / "packaging/version_info.txt"))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="DocQA")
