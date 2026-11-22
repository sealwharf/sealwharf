from io import BytesIO

import pytest
from PIL import Image
from pypdf import PdfReader

from sealwharf.documents.images import ImageError, image_to_pdf, is_image


def picture(width, height, kind="PNG", **options):
    out = BytesIO()
    Image.new("RGB", (width, height), "white").save(out, kind, **options)
    return out.getvalue()


def test_a_picture_becomes_one_a4_page():
    for kind in ("PNG", "JPEG", "WEBP"):
        data = picture(1200, 1600, kind)
        assert is_image(data)
        pages = PdfReader(BytesIO(image_to_pdf(data))).pages
        assert len(pages) == 1
        assert (round(float(pages[0].mediabox.width)), round(float(pages[0].mediabox.height))) == (595, 842)


def test_a_wide_picture_gets_a_landscape_page():
    page = PdfReader(BytesIO(image_to_pdf(picture(1600, 1200)))).pages[0]
    assert float(page.mediabox.width) > float(page.mediabox.height)


def test_the_camera_orientation_is_followed():
    exif = Image.Exif()
    exif[0x0112] = 6  # taken with the phone turned: the stored picture is wide, the document is tall
    page = PdfReader(BytesIO(image_to_pdf(picture(1600, 1200, "JPEG", exif=exif.tobytes())))).pages[0]
    assert float(page.mediabox.height) > float(page.mediabox.width)


def test_what_is_not_a_small_enough_picture_is_refused():
    assert not is_image(b"%PDF-1.7") and not is_image(b"GIF89a")
    with pytest.raises(ImageError):
        image_to_pdf(b"\x89PNG\r\n\x1a\n" + b"broken")
    with pytest.raises(ImageError):
        image_to_pdf(picture(8000, 6000))  # 48 million pixels
