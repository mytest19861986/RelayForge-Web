"""Validation shared by uploads and the isolated renderer."""

import io
import warnings

from PIL import Image

Image.MAX_IMAGE_PIXELS = 40_000_000
MAX_BYTES = 32 * 1024 * 1024
IMAGE_TYPES = {"PNG", "JPEG", "WEBP", "GIF"}


def media_format(data):
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("MEDIA_SIZE_INVALID")
    prefix = data[:32]
    if prefix.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or (
        prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"
    ):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data), formats=sorted(IMAGE_TYPES)) as image:
                    kind, dimensions = image.format, image.size
                    image.verify()
                    return kind, dimensions
        except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, SyntaxError):
            raise ValueError("INVALID_IMAGE") from None
    if prefix[4:8] == b"ftyp" and len(prefix) >= 16:
        return "MP4", None
    if prefix.startswith(b"OggS\x00"):
        return "OGG", None
    if prefix.startswith(b"ID3") or (
        len(prefix) >= 4 and prefix[0] == 255 and prefix[1] & 224 == 224
    ):
        return "MP3", None
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return "WAV", None
    raise ValueError("MEDIA_FORMAT_INVALID")
