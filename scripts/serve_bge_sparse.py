"""Explicit local-weight launcher; no implicit model downloads."""

import argparse
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from docqa.sparse_service import create_app  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--model-path", required=True, type=Path)
parser.add_argument("--device", default="cpu")
parser.add_argument("--revision", default="local-v1")
parser.add_argument("--port", default=18100, type=int)
args = parser.parse_args()
uvicorn.run(
    create_app(args.model_path, device=args.device, revision=args.revision),
    host="127.0.0.1",
    port=args.port,
    log_level="warning",
)
