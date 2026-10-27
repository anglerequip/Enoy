"""Images attached to chat messages.

Images are stored once in the app data folder, named by their SHA-256 hash, and
messages refer to them by that id. Only PNG and JPEG are accepted: both Codex
and Claude adapters can read them. The chat window converts other formats
and shrinks large pictures before uploading.
"""

from __future__ import annotations

import base64
import hashlib
import re

from . import paths
from .backends.base import ImageData

MAX_BYTES = 3_500_000      # Fits Claude's 5 MB limit once base64-encoded, and the 5 MB request limit.
MAX_PER_MESSAGE = 5
_ID = re.compile(r"^[0-9a-f]{64}$")
_EXT = {"image/png": "png", "image/jpeg": "jpg"}


class ImageError(ValueError):
    pass


def media_type(data: bytes) -> str | None:
    """Detect the format from the file's first bytes, not from its name."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def decode_upload(text: str) -> bytes:
    """Accept plain base64 or a data: URL from the browser."""
    if not isinstance(text, str) or not text:
        raise ImageError("No image data received.")
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    try:
        return base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise ImageError("The image data is not valid base64.") from exc


def store(data: bytes) -> dict:
    kind = media_type(data)
    if not kind:
        raise ImageError("Only PNG and JPEG images are supported.")
    if len(data) > MAX_BYTES:
        raise ImageError(f"The image is too large ({len(data) / 1e6:.1f} MB). The limit is {MAX_BYTES / 1e6:.1f} MB.")
    image_id = hashlib.sha256(data).hexdigest()
    target = paths.images_dir() / f"{image_id}.{_EXT[kind]}"
    if not target.exists():
        target.write_bytes(data)
    return {"id": image_id, "media_type": kind, "bytes": len(data)}


def valid_ids(ids) -> list[str]:
    """Image ids from a request, checked and de-duplicated. Unknown ids are an error."""
    out: list[str] = []
    for image_id in ids or []:
        if not isinstance(image_id, str) or not _ID.match(image_id):
            raise ImageError("Invalid image id.")
        if image_id not in out:
            if _path(image_id) is None:
                raise ImageError("An attached image was not found. Attach it again.")
            out.append(image_id)
    if len(out) > MAX_PER_MESSAGE:
        raise ImageError(f"At most {MAX_PER_MESSAGE} images per message.")
    return out


def _path(image_id: str):
    if not _ID.match(image_id or ""):
        return None
    for ext in _EXT.values():
        p = paths.images_dir() / f"{image_id}.{ext}"
        if p.is_file():
            return p
    return None


def load(image_id: str) -> ImageData:
    p = _path(image_id)
    if p is None:
        raise ImageError("Image not found.")
    data = p.read_bytes()
    return ImageData(media_type(data) or "image/png", data)
