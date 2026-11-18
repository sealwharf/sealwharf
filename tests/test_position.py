from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject

from sealwharf.documents.position import find_signature_position


def make_pdf(path: Path, fields: list[tuple[str, tuple[int, int, int, int], int]], rotation: int = 0) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    page[NameObject("/Rotate")] = NumberObject(rotation)
    annotations = ArrayObject()
    for name, rect, flags in fields:
        field = DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Sig"),
            NameObject("/T"): TextStringObject(name),
            NameObject("/Rect"): ArrayObject([NumberObject(v) for v in rect]),
            NameObject("/F"): NumberObject(flags),
        })
        annotations.append(writer._add_object(field))
    page[NameObject("/Annots")] = annotations
    with path.open("wb") as stream:
        writer.write(stream)


def test_only_exact_identity_field_is_selected(tmp_path: Path) -> None:
    pdf = tmp_path / "test.pdf"
    make_pdf(pdf, [("Assinatura Joao da Silva", (50, 100, 250, 150), 4),
                   ("Assinatura Maria Silva", (50, 200, 250, 250), 4)])
    result = find_signature_position(pdf, "Jo\u00e3o da Silva")
    assert result is not None
    assert (result.page, result.box, result.field_name) == (0, (50, 100, 250, 150), "Assinatura Joao da Silva")


def test_ambiguous_fields_require_review(tmp_path: Path) -> None:
    pdf = tmp_path / "test.pdf"
    make_pdf(pdf, [("Joao Silva 1", (50, 100, 250, 150), 4),
                   ("Joao Silva 2", (50, 200, 250, 250), 4)])
    assert find_signature_position(pdf, "Joao Silva") is None


def test_missing_or_partial_identity_requires_review(tmp_path: Path) -> None:
    pdf = tmp_path / "test.pdf"
    make_pdf(pdf, [("Joao Silva", (50, 100, 250, 150), 4)])
    assert find_signature_position(pdf, "Joao") is None
    assert find_signature_position(pdf, "Joao da Silva") is None


def test_unique_field_without_identity_filter(tmp_path: Path) -> None:
    pdf = tmp_path / "test.pdf"
    make_pdf(pdf, [("Signature field", (50, 100, 250, 150), 4)])
    assert find_signature_position(pdf) is not None
    make_pdf(pdf, [("First", (50, 100, 250, 150), 4), ("Second", (50, 200, 250, 250), 4)])
    assert find_signature_position(pdf) is None


def test_full_name_does_not_match_a_different_longer_name(tmp_path: Path) -> None:
    pdf = tmp_path / "test.pdf"
    make_pdf(pdf, [("Jane Example Junior", (50, 100, 250, 150), 4)])
    assert find_signature_position(pdf, "Jane Example") is None


def test_unsafe_geometry_or_hidden_widget_requires_review(tmp_path: Path) -> None:
    pdf = tmp_path / "test.pdf"
    for rect, flags, rotation in [((-10, 100, 250, 150), 4, 0),
                                  ((50, 100, 51, 150), 4, 0),
                                  ((50, 100, 250, 150), 2, 0),
                                  ((50, 100, 250, 150), 4, 90)]:
        make_pdf(pdf, [("Joao Silva", rect, flags)], rotation)
        assert find_signature_position(pdf, "Joao Silva") is None


def test_initialed_middle_name_selects_field_unless_ambiguous(tmp_path: Path) -> None:
    pdf = tmp_path / "test.pdf"
    make_pdf(pdf, [("Assinatura Jane M. Example", (50, 100, 250, 150), 4)])
    assert find_signature_position(pdf, "Jane Maria Example") is not None
    assert find_signature_position(pdf, "Jane Paula Example") is None
    make_pdf(pdf, [("Jane M. Example", (50, 100, 250, 150), 4), ("Jane Maria Example", (50, 200, 250, 250), 4)])
    assert find_signature_position(pdf, "Jane Maria Example") is None
