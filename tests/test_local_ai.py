import base64
import json
from io import BytesIO

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from sealwharf.adapters.local_ai import suggest_local_ai_position


def document(label=b"Jane Example"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=600)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (" + label + b") Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def response(verdict):
    return httpx.Response(200, json={"done": True, "message": {"content": json.dumps(verdict)}})


def test_local_model_can_confirm_only_prevalidated_candidate():
    calls = []
    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert str(request.url) == "http://127.0.0.1:11434/api/chat"
        assert body["model"] == "qwen3-vl:4b"
        assert base64.b64decode(body["messages"][0]["images"][0]).startswith(b"\x89PNG\r\n\x1a\n")
        return response({"signature_area": True, "visible_name": "Jane Example"})
    candidate = suggest_local_ai_position(document(), "Jane Example", transport=httpx.MockTransport(handler))
    assert candidate is not None
    assert candidate.page == 0 and candidate.box == (100, 202, 250, 242)
    assert len(calls) == 1


def test_does_not_send_document_to_model_if_identity_does_not_match():
    def handler(_):
        raise AssertionError("AI must not be called without an independently validated candidate")
    assert suggest_local_ai_position(document(), "John Example", transport=httpx.MockTransport(handler)) is None


def test_uncertain_malformed_and_failed_model_responses_are_rejected():
    for verdict in ({"signature_area": False, "visible_name": "Jane Example"},
                    {"signature_area": True, "visible_name": "John Example"},
                    {"signature_area": True, "visible_name": "Jane Example", "page": 9}):
        transport = httpx.MockTransport(lambda _, value=verdict: response(value))
        assert suggest_local_ai_position(document(), "Jane Example", transport=transport) is None
    assert suggest_local_ai_position(document(), "Jane Example", transport=httpx.MockTransport(lambda _: httpx.Response(500))) is None


def scanned_document(*, occupied=False):
    from PIL import Image, ImageDraw
    from pypdf.generic import NumberObject
    picture = Image.new("RGB", (400, 600), "white")
    draw = ImageDraw.Draw(picture)
    draw.line((100, 400, 250, 400), fill="black", width=1)
    if occupied:
        draw.rectangle((120, 370, 220, 390), fill="black")
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=600)
    image = DecodedStreamObject()
    image.set_data(picture.tobytes())
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(400), NameObject("/Height"): NumberObject(600),
                  NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): writer._add_object(image)})})
    stream = DecodedStreamObject()
    stream.set_data(b"q 400 0 0 600 0 0 cm /Im0 Do Q")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def scan_transport(name="Jane Example", box=(250, 600, 625, 660)):
    def handler(request):
        schema = json.loads(request.content)["format"]
        if "box" in schema["properties"]:
            return response({"found": True, "visible_name": name, "box": list(box)})
        return response({"signature_area": True, "visible_name": name})
    return httpx.MockTransport(handler)


def test_scanned_page_is_located_by_the_model_and_checked_on_pixels():
    from sealwharf.adapters.local_ai import suggest_general_position
    candidate = suggest_general_position(scanned_document(), "Jane Example", transport=scan_transport())
    assert candidate is not None and candidate.reason == "ai_proposal"
    assert candidate.box == (100.0, 204.0, 250.0, 240.0)


def test_scanned_page_is_refused_when_area_has_ink_or_name_differs():
    from sealwharf.adapters.local_ai import suggest_general_position
    assert suggest_general_position(scanned_document(occupied=True), "Jane Example", transport=scan_transport()) is None
    assert suggest_general_position(scanned_document(), "Jane Example", transport=scan_transport(box=(250, 640, 625, 700))) is None
    assert suggest_general_position(scanned_document(), "Jane Example", transport=scan_transport(name="John Example")) is None


def test_textless_page_that_is_not_a_scan_is_never_sent_to_the_model():
    from sealwharf.adapters.local_ai import suggest_general_position
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=600)
    stream = DecodedStreamObject()
    stream.set_data(b"100 200 m 250 200 l S")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)

    def handler(_):
        raise AssertionError("The model must not be called")
    assert suggest_general_position(output.getvalue(), "Jane Example", transport=httpx.MockTransport(handler)) is None
