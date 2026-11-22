"""Turn a picture of a document (PNG, JPEG or WebP) into a one-page PDF that can be signed.

The picture is placed on an A4 page, turned upright as the camera recorded it
and reduced to 200 dots per inch. Its size and pixel count are checked before
it is decoded, so a small file that would unpack into a huge picture is refused.
The page has no text, so it is read like a scanned page.
"""

from io import BytesIO

from PIL import Image, ImageOps

from .. import limits

MAX_PIXELS = 40_000_000
FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
DPI = 200
A4 = (8.27, 11.69)  # inches


class ImageError(Exception):
    """The picture is not a supported image, or is too large."""


def is_image(data: bytes) -> bool:
    """Whether the file starts like a PNG, JPEG or WebP picture."""
    return data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def image_to_pdf(data: bytes) -> bytes:
    """Place the picture on an A4 page, upright, and return the page as a PDF."""
    if len(data) > limits.current.max_file_bytes or not is_image(data):
        raise ImageError("Unsupported or oversized image.")
    try:
        with Image.open(BytesIO(data)) as picture:
            if picture.format not in FORMATS or picture.width * picture.height > MAX_PIXELS:
                raise ImageError("Unsupported or oversized image.")
            picture.load()
            upright = ImageOps.exif_transpose(picture).convert("RGB")
    except ImageError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageError("The image could not be read.") from exc
    landscape = upright.width > upright.height
    inches = (A4[1], A4[0]) if landscape else A4
    page = Image.new("RGB", (round(inches[0] * DPI), round(inches[1] * DPI)), "white")
    scale = min(page.width / upright.width, page.height / upright.height, 1.0)
    fitted = upright.resize((max(1, round(upright.width * scale)), max(1, round(upright.height * scale))), Image.LANCZOS)
    page.paste(fitted, ((page.width - fitted.width) // 2, (page.height - fitted.height) // 2))
    output = BytesIO()
    page.save(output, "PDF", resolution=DPI)
    return output.getvalue()
