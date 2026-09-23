"""Decode untrusted images in a bounded process without credentials."""

import io
import resource
import sys
import warnings

from PIL import Image, ImageOps


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    Image.MAX_IMAGE_PIXELS = 25_000_000
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    body = sys.stdin.buffer.read(25 * 1024 * 1024 + 1)
    if len(body) > 25 * 1024 * 1024:
        raise ValueError()
    with Image.open(io.BytesIO(body)) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((2000, 2000))
        image.save(sys.stdout.buffer, format="PNG")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(1) from None
