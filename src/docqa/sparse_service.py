"""Optional loopback BGE-M3 learned sparse encoder, compatible with DocBase HTTP v1.

Importing does not load/download weights. Production requires an explicitly
supplied local model directory; tests inject a synthetic encoder.
"""

import hmac
import json
import math
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from docqa.domain.documents import Contract


class SparseInput(Contract):
    model: str
    revision: str
    tokenizer_revision: str
    input: list[str] = Field(min_length=1, max_length=32)
    input_type: Literal["document", "query"]
    return_sparse: Literal[True] = True
    truncate: Literal[False] = False


def create_app(model_path=None, *, encoder=None, revision="local-v1", device="cpu"):
    model_id = "BAAI/bge-m3"
    lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        model = encoder
        if model is None:
            path = Path(model_path or "").resolve()
            if not model_path or not path.is_dir():
                raise RuntimeError("Supply an existing local BGE-M3 model directory")
            from FlagEmbedding import BGEM3FlagModel

            model = BGEM3FlagModel(str(path), use_fp16=device != "cpu", devices=device)
        app.state.encoder = model
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    def authenticated(request):
        secret = os.environ.get("DOCQA_BGE_SPARSE_KEY")
        if secret and not hmac.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + secret
        ):
            raise HTTPException(401, "Credential required")

    @app.get("/health")
    def health(request: Request):
        authenticated(request)
        return {
            "ready": True,
            "model": model_id,
            "revision": revision,
            "tokenizer_revision": revision,
            "vocab_size": len(app.state.encoder.tokenizer),
            "kind": "learned_sparse",
        }

    def encode(value):
        model = app.state.encoder
        with lock:
            if any(
                not text.strip()
                or len(text.encode("utf8")) > 32768
                or len(model.tokenizer.encode(text, add_special_tokens=True, truncation=False))
                > 8192
                for text in value.input
            ):
                raise HTTPException(
                    422, "Input is empty or exceeds encoder budget; truncation is disabled"
                )
            output = model.encode(
                value.input,
                batch_size=8,
                max_length=8192,
                return_dense=False,
                return_sparse=True,
                return_colbert_vecs=False,
            )
        weights = output["lexical_weights"]
        if len(weights) != len(value.input):
            raise HTTPException(503, "Invalid encoder output")
        rows = []
        for index, sparse in enumerate(weights):
            try:
                values = {str(key): float(weight) for key, weight in sparse.items()}
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(503, "Invalid encoder weights") from None
            if not values or any(
                not key.isascii()
                or not key.isdecimal()
                or not math.isfinite(weight)
                or weight < 0
                or not 0 <= int(key) < len(model.tokenizer)
                for key, weight in values.items()
            ):
                raise HTTPException(503, "Invalid encoder weights")
            result = {key: weight for key, weight in values.items() if weight > 0}
            if not result:
                raise HTTPException(503, "Empty encoder weights")
            rows.append({"index": index, "lexical_weights": result})
        return {"data": rows, "model": model_id, "revision": revision}

    @app.post("/sparse")
    async def sparse(request: Request):
        authenticated(request)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "JSON required")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1_048_576:
                raise HTTPException(413, "Request too large")
        try:
            value = SparseInput.model_validate(json.loads(raw))
        except (ValueError, UnicodeError):
            raise HTTPException(422, "Invalid request") from None
        if (
            value.model != model_id
            or value.revision != revision
            or value.tokenizer_revision != revision
        ):
            raise HTTPException(409, "Encoder identity differs")
        return await run_in_threadpool(encode, value)

    return app
