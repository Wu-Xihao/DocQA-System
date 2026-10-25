"""Deterministic IDs and normalized image assets shared by format adapters."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from docqa.domain.documents import ImageAsset
from docqa.domain.errors import DocQAError


def digest(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def safe_slug(value: str) -> str:
    return re.sub(r"[^\w\-]+", "_", value, flags=re.UNICODE).strip("._")[:60] or "document"


@dataclass
class ParseContext:
    source: Path
    work_dir: Path
    doc_id: str
    revision_id: str
    source_name: str
    max_pages: int = 1000
    render_dpi: int = 144
    max_pixels: int = 20_000_000
    render_pdf: bool = True
    raw_assets: dict[str, ImageAsset] = field(default_factory=dict)
    image_assets: dict[str, ImageAsset] = field(default_factory=dict)

    def identity(self, *parts) -> str:
        return digest([self.revision_id, *parts])

    def save_image(self, data: bytes, label: str, *, derived_from: str | None = None) -> ImageAsset:
        try:
            with Image.open(BytesIO(data)) as image:
                if image.width * image.height > self.max_pixels:
                    raise DocQAError(
                        "RESOURCE_LIMIT_EXCEEDED", "Image exceeds configured pixel limit"
                    )
                original_format = image.format or "BIN"
                original_size = image.size
                image = ImageOps.exif_transpose(image).convert("RGB")
                output = BytesIO()
                image.save(output, format="PNG")
                width, height = image.size
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise DocQAError("IMAGE_DECODE_FAILED", "Embedded image cannot be decoded") from exc
        normalized = output.getvalue()
        checksum = sha256(normalized).hexdigest()
        raw_checksum = sha256(data).hexdigest()
        if raw_checksum != checksum:
            extension = {
                "JPEG": "jpg",
                "PNG": "png",
                "GIF": "gif",
                "TIFF": "tiff",
                "BMP": "bmp",
                "WEBP": "webp",
            }.get(original_format, "bin")
            raw_relative = f"images/original/{raw_checksum}.{extension}"
            raw_target = self.work_dir / raw_relative
            raw_target.parent.mkdir(parents=True, exist_ok=True)
            raw_target.write_bytes(data)
            self.raw_assets[raw_checksum] = ImageAsset(
                asset_id=raw_checksum,
                sha256=raw_checksum,
                relative_path=raw_relative,
                media_type=Image.MIME.get(original_format, "application/octet-stream"),
                width=original_size[0],
                height=original_size[1],
            )
        if checksum in self.image_assets:
            return self.image_assets[checksum].model_copy(
                update={
                    "derived_from": derived_from
                    or (raw_checksum if raw_checksum != checksum else None)
                }
            )
        relative = f"images/{safe_slug(Path(self.source_name).stem)}__{safe_slug(label)}__{checksum[:16]}.png"
        target = self.work_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(normalized)
        asset = ImageAsset(
            asset_id=checksum,
            sha256=checksum,
            relative_path=relative,
            media_type="image/png",
            width=width,
            height=height,
            derived_from=derived_from or (raw_checksum if raw_checksum != checksum else None),
        )
        self.image_assets[checksum] = asset
        return asset
