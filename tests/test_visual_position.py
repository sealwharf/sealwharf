from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from sealwharf.documents.visual_position import suggest_visual_positions


def make_pdf(content: bytes, rotation: int = 0, annotation: bool = False) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    image = DecodedStreamObject()
    image.set_data(b"\xff\xff\xff")
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1),
                  NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): writer._add_object(image)})
    })
    if annotation:
        note = DictionaryObject({NameObject("/Type"): NameObject("/Annot"),
                                 NameObject("/Subtype"): NameObject("/Text"),
                                 NameObject("/Rect"): ArrayObject([NumberObject(120), NumberObject(215), NumberObject(140), NumberObject(230)])})
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(note)])
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Rotate")] = NumberObject(rotation)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_proposes_labeled_drawn_line_only_for_review():
    pdf = make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Jane Example) Tj ET")
    candidates = suggest_visual_positions(pdf)
    assert len(candidates) == 1
    assert candidates[0].page == 0
    assert candidates[0].box == (100, 202, 250, 242)
    assert candidates[0].reason == "line_with_label"


def test_proposes_word_style_underscore_line():
    pdf = make_pdf(b"BT /F1 12 Tf 100 200 Td (____________________) Tj ET "
                   b"BT /F1 10 Tf 125 185 Td (Someone Else) Tj ET")
    result = suggest_visual_positions(pdf)
    assert len(result) == 1
    assert result[0].box[0] == 100
    assert result[0].box[1] == 202
    assert 200 < result[0].box[2] < 230


def test_rejects_unlabeled_short_diagonal_or_rotated_lines():
    for content in [b"100 200 m 250 200 l S",
                    b"100 200 m 120 200 l S BT /F1 10 Tf 100 185 Td (Name Here) Tj ET",
                    b"100 200 m 250 210 l S BT /F1 10 Tf 100 185 Td (Name Here) Tj ET"]:
        assert suggest_visual_positions(make_pdf(content)) == []
    assert suggest_visual_positions(make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Name Here) Tj ET", 90)) == []


def test_rejects_multiple_candidates_instead_of_selecting_one():
    pdf = make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Person One) Tj ET "
                   b"100 400 m 250 400 l S BT /F1 10 Tf 140 385 Td (Person Two) Tj ET")
    assert suggest_visual_positions(pdf) == []
    with pytest.raises(ValueError):
        suggest_visual_positions(pdf, max_candidates=1)


def test_excludes_boxes_outside_page():
    pdf = make_pdf(b"100 770 m 250 770 l S BT /F1 10 Tf 140 755 Td (Person One) Tj ET")
    assert suggest_visual_positions(pdf) == []


@pytest.mark.parametrize("obstacle", [
    b"BT /F1 10 Tf 140 220 Td (Occupied) Tj ET",
    b"120 220 m 200 220 l S",
    b"q 40 0 0 20 140 215 cm /Im0 Do Q",
])
def test_rejects_existing_text_graphic_or_image_over_candidate(obstacle):
    line = b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Person One) Tj ET "
    assert suggest_visual_positions(make_pdf(line + obstacle)) == []


def test_rejects_annotation_over_candidate():
    line = b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Person One) Tj ET"
    assert suggest_visual_positions(make_pdf(line, annotation=True)) == []


@pytest.mark.parametrize("border", [
    b"100 370 150 30 re S ",
    b"100 370 m 100 400 l S 250 370 m 250 400 l S ",
])
def test_excludes_table_borders_without_hiding_a_free_signature_line(border):
    table = b"100 400 m 250 400 l S BT /F1 10 Tf 140 385 Td (Table Header) Tj ET " + border
    assert suggest_visual_positions(make_pdf(table)) == []
    table += b"100 370 m 250 370 l S BT /F1 10 Tf 140 355 Td (Table Value) Tj ET "
    table += b"100 340 150 30 re S "
    signature = (b"BT /F1 12 Tf 100 200 Td (____________________) Tj ET "
                 b"BT /F1 10 Tf 125 185 Td (Someone Else) Tj ET")
    candidates = suggest_visual_positions(make_pdf(table + signature))
    assert len(candidates) == 1
    assert candidates[0].box[1] == 202


def test_does_not_discard_a_single_framed_line_to_select_another():
    framed = b"100 400 m 250 400 l S BT /F1 10 Tf 140 385 Td (Person One) Tj ET 100 370 150 30 re S "
    free = b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Person Two) Tj ET"
    assert suggest_visual_positions(make_pdf(framed + free)) == []


def test_does_not_discard_a_framed_line_on_another_page():
    framed = b"100 400 m 250 400 l S BT /F1 10 Tf 140 385 Td (Person One) Tj ET 100 370 150 30 re S "
    free = b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Person Two) Tj ET"
    writer = PdfWriter()
    writer.add_page(PdfReader(BytesIO(make_pdf(framed))).pages[0])
    writer.add_page(PdfReader(BytesIO(make_pdf(free))).pages[0])
    output = BytesIO()
    writer.write(output)
    assert suggest_visual_positions(output.getvalue()) == []


def test_selects_only_the_requested_signer_among_multiple_lines():
    pdf = make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Jane Example) Tj ET "
                   b"100 400 m 250 400 l S BT /F1 10 Tf 140 385 Td (John Example) Tj ET")
    result = suggest_visual_positions(pdf, signer_name="Jane Example")
    assert len(result) == 1
    assert result[0].box == (100, 202, 250, 242)
    assert suggest_visual_positions(pdf, signer_name="Another Person") == []


def test_name_matching_ignores_accents_but_not_missing_or_extra_names():
    pdf = make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Jose Silva) Tj ET")
    assert len(suggest_visual_positions(pdf, signer_name="Jos\u00e9 Silva")) == 1
    assert suggest_visual_positions(pdf, signer_name="Jose") == []
    assert suggest_visual_positions(pdf, signer_name="Jose da Silva") == []
    assert suggest_visual_positions(pdf, signer_name="Jose Silva Junior") == []


def test_identity_does_not_select_a_partial_or_unnamed_signature():
    longer_name = make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 120 185 Td (Jane Example Junior) Tj ET")
    generic = make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Signature) Tj ET")
    assert suggest_visual_positions(longer_name, signer_name="Jane Example") == []
    assert suggest_visual_positions(generic, signer_name="Jane Example") == []


def test_identity_accepts_known_name_label_but_not_additional_words():
    labeled = make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 105 185 Td (Nome completo: Jane Example) Tj ET")
    extra = make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 105 185 Td (Jane Example signing for John) Tj ET")
    assert len(suggest_visual_positions(labeled, signer_name="Jane Example")) == 1
    assert suggest_visual_positions(extra, signer_name="Jane Example") == []


def test_duplicate_signer_and_competing_labels_are_ambiguous():
    duplicate = make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Jane Example) Tj ET "
                         b"100 400 m 250 400 l S BT /F1 10 Tf 140 385 Td (Jane Example) Tj ET")
    competing = make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 120 185 Td (Jane Example) Tj ET "
                         b"BT /F1 10 Tf 170 180 Td (John Example) Tj ET")
    assert suggest_visual_positions(duplicate, signer_name="Jane Example") == []
    assert suggest_visual_positions(competing, signer_name="Jane Example") == []


def test_identity_does_not_bypass_collision_checks():
    pdf = make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Jane Example) Tj ET "
                   b"BT /F1 10 Tf 140 220 Td (Occupied) Tj ET")
    assert suggest_visual_positions(pdf, signer_name="Jane Example") == []


def test_identity_accepts_initialed_middle_names_but_stays_unambiguous():
    abbreviated = make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 120 185 Td (Jane M. Example) Tj ET")
    duplicate = make_pdf(b"100 200 m 250 200 l S BT /F1 10 Tf 120 185 Td (Jane M. Example) Tj ET "
                         b"100 400 m 250 400 l S BT /F1 10 Tf 120 385 Td (Jane Maria Example) Tj ET")
    assert len(suggest_visual_positions(abbreviated, signer_name="Jane Maria Example")) == 1
    assert suggest_visual_positions(abbreviated, signer_name="Jane Paula Example") == []
    assert suggest_visual_positions(abbreviated, signer_name="Jane Example") == []
    assert suggest_visual_positions(duplicate, signer_name="Jane Maria Example") == []


def test_rotated_page_is_normalized_before_locating_the_line():
    from sealwharf.documents.normalize import normalize_rotation
    upright = b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Jane Example) Tj ET"
    # Content authored for a page displayed with a 90 degree rotation.
    rotated = make_pdf(b"0 1 -1 0 600 0 cm " + upright, rotation=90)
    assert suggest_visual_positions(rotated, signer_name="Jane Example") == []
    normalized = normalize_rotation(rotated)
    page = PdfReader(BytesIO(normalized)).pages[0]
    assert int(page.rotation or 0) == 0 and tuple(float(v) for v in page.mediabox) == (0, 0, 800, 600)
    result = suggest_visual_positions(normalized, signer_name="Jane Example")
    assert len(result) == 1 and result[0].box == (100, 202, 250, 242)
    # Text that stays sideways after normalization never labels a line.
    sideways = normalize_rotation(make_pdf(upright, rotation=90))
    assert suggest_visual_positions(sideways, signer_name="Jane Example") == []


def test_normalization_leaves_unrotated_annotated_or_unreadable_documents_unchanged():
    from sealwharf.documents.normalize import normalize_rotation
    content = b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Jane Example) Tj ET"
    for document in [make_pdf(content), make_pdf(content, rotation=90, annotation=True), b"not a pdf"]:
        assert normalize_rotation(document) is document


LINE = b"100 200 m 250 200 l S "


@pytest.mark.parametrize("label", [
    b"BT 3 Tr /F1 10 Tf 140 185 Td (Jane Example) Tj ET",  # invisible render mode
    b"BT 7 Tr /F1 10 Tf 140 185 Td (Jane Example) Tj ET",  # clipping only, never painted
    b"BT 3 Tr /F1 10 Tf 140 185 Td (Jane Example) Tj 0 Tr ( ) Tj ET",  # hidden name, visible blank after it
    b"BT /F1 10 Tf 140 185 Td (Jane ) Tj 3 Tr (Example) Tj ET",  # half of the name hidden
    b"1 g BT /F1 10 Tf 140 185 Td (Jane Example) Tj ET",  # white gray fill
    b"1 1 1 rg BT /F1 10 Tf 140 185 Td (Jane Example) Tj ET",  # white RGB fill
    b"0 0 0 0 k BT /F1 10 Tf 140 185 Td (Jane Example) Tj ET",  # white CMYK fill
    b"BT /F1 0.5 Tf 140 195 Td (Jane Example) Tj ET",  # unreadably small
    b"BT /F1 10 Tf 1 0 0 0.1 140 190 Tm (Jane Example) Tj ET",  # squashed to one point high
])
def test_text_a_reader_cannot_see_never_labels_a_line(label):
    assert suggest_visual_positions(make_pdf(LINE + label), signer_name="Jane Example") == []


def test_visibility_is_restored_with_the_graphics_state():
    content = LINE + b"q 3 Tr 1 g /F1 0.5 Tf Q BT /F1 10 Tf 140 185 Td (Jane Example) Tj ET"
    result = suggest_visual_positions(make_pdf(content), signer_name="Jane Example")
    assert len(result) == 1 and result[0].box == (100, 202, 250, 242)


def test_visible_label_after_hidden_text_object_still_counts():
    content = LINE + b"BT 3 Tr /F1 10 Tf 300 500 Td (Hidden Note) Tj ET BT 0 Tr /F1 10 Tf 140 185 Td (Jane Example) Tj ET"
    result = suggest_visual_positions(make_pdf(content), signer_name="Jane Example")
    assert len(result) == 1


def test_hidden_underscores_do_not_draw_a_line():
    content = b"BT 3 Tr /F1 12 Tf 100 200 Td (____________________) Tj ET BT /F1 10 Tf 125 185 Td (Jane Example) Tj ET"
    assert suggest_visual_positions(make_pdf(content), signer_name="Jane Example") == []
