"""Manually verify the native joint API with two fixed, non-business test sentences."""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from docqa.docbase.config import load_options  # noqa: E402
from docqa.docbase.dashscope import shared_encoder  # noqa: E402
from docqa.docbase.providers import make_providers  # noqa: E402
from docqa.domain.errors import DocQAError  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/docbase.example.yaml")
    )
    args = parser.parse_args()
    try:
        options = load_options(args.config)
        if options.mode != "http":
            raise DocQAError("INVALID_CONFIG", "Select native HTTP configuration")
        providers = make_providers(options)
        encoder = shared_encoder(*providers[:2])
        if encoder is None:
            raise DocQAError("INVALID_CONFIG", "Select DashScope joint embedding configuration")
        started = time.perf_counter()
        vectors = encoder.encode(
            [
                "DocQA synthetic heartbeat check.",
                "DocQA synthetic document parsing check.",
            ],
            "query",
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "model": options.dense.model,
                    "synthetic_input": True,
                    "output_type": "dense&sparse",
                    "text_type": "query",
                    "texts": len(vectors),
                    "dense_dimensions": [len(value["dense"]) for value in vectors],
                    "sparse_nonzeros": [len(value["sparse"]) for value in vectors],
                    "seconds": round(time.perf_counter() - started, 3),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except DocQAError as exc:
        # Never print credentials, provider error bodies or business content.
        print(json.dumps({"status": "failed", "error_code": exc.code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
