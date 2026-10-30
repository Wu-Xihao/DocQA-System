"""Single source of truth for the document formats each layer accepts.

Service, scanner, Web upload and HTTP filters all read these tuples so a new
parser is enabled everywhere at once instead of drifting per layer.
"""

from __future__ import annotations

NATIVE_FORMATS = ("pdf", "docx", "pptx", "xlsx", "csv")
# Legacy binary Office files are converted to PDF through LibreOffice first.
LEGACY_FORMATS = ("doc", "ppt")
ALL_FORMATS = NATIVE_FORMATS + LEGACY_FORMATS
# Formats a browser upload or folder registration accepts by default.
UPLOAD_FORMATS = NATIVE_FORMATS


def normalize_types(values, allowed=ALL_FORMATS) -> list[str]:
    """Lower-case, strip dots and reject anything outside the allowed set."""
    result = [str(value).strip().lower().lstrip(".") for value in values]
    if not result or not set(result).issubset(allowed):
        raise ValueError("Types must be a nonempty selection of " + ",".join(allowed))
    return list(dict.fromkeys(result))
