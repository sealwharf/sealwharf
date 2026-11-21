import json
from io import BytesIO

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from sealwharf.adapters.local_ai import NothingToChoose, choose_signature_place
from sealwharf.documents.candidates import describe_places, free_places_at_end, place_by_name, signature_places


def page(*texts, lines=b"", boxes=b""):
    """One page with text at (x, y), drawn lines and bordered boxes; every name is fictitious."""
    writer = PdfWriter()
    sheet = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    sheet[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = lines + boxes + b"".join(b"BT /F1 11 Tf %d %d Td (%s) Tj ET " % (x, y, words) for x, y, words in texts)
    stream = DecodedStreamObject()
    stream.set_data(content)
    sheet[NameObject("/Contents")] = writer._add_object(stream)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def framed(bottom, top):
    return b"50 %d m 50 %d l S 550 %d m 550 %d l S 50 %d m 550 %d l S 50 %d m 550 %d l S " % (bottom, top, bottom, top, bottom, bottom, top, top)


# Two framed forms, one per person, each with its own "Signature:" label.
FORMS = page((60, 720, b"Name: Jane Example"), (60, 650, b"Signature:"),
             (60, 520, b"Entity representative: John Example"), (60, 450, b"Signature of the entity representative:"),
             boxes=framed(630, 760) + framed(430, 560))


def test_each_place_comes_with_the_text_of_its_own_frame():
    places = signature_places(FORMS)
    assert [place.letter for place in places] == ["A", "B"]
    assert "Name: Jane Example" in places[0].context and "Name: Jane Example" not in places[1].context
    assert place_by_name(places, "Jane Example") == places[0]
    assert place_by_name(places, "John Example") == places[1]
    assert place_by_name(places, "Mary Major") is None
    assert "[A] page 1, label: Signature:" in describe_places(places)


def test_a_role_in_the_label_must_be_the_role_written_with_the_name():
    witness = page((60, 720, b"Witness: Jane Example"), (60, 650, b"Signature of the entity representative:"), boxes=framed(630, 760))
    assert place_by_name(signature_places(witness), "Jane Example") is None


def test_a_name_inside_a_sentence_does_not_claim_the_place_next_to_it():
    declaration = page((60, 700, b"I declare that Jane Example took part in every activity of the project this year."),
                       (60, 650, b"Signature:"))
    places = signature_places(declaration)
    assert len(places) == 1 and place_by_name(places, "Jane Example") is None


def test_the_name_under_a_line_claims_the_place_above_it():
    lines = b"100 500 m 280 500 l S 100 300 m 280 300 l S "
    signed = page((110, 485, b"Jane Example"), (110, 285, b"John Example"), lines=lines)
    places = signature_places(signed)
    jane, john = place_by_name(places, "Jane Example"), place_by_name(places, "John Example")
    assert jane is not None and john is not None and jane.box[1] > 400 > john.box[1]
    # Two names on one text line, side by side, cannot be told apart here: neither place is claimed.
    side = page((110, 285, b"Jane Example"), (340, 285, b"John Example"), lines=b"100 300 m 280 300 l S 330 300 m 510 300 l S ")
    assert place_by_name(signature_places(side), "Jane Example") is None


def reply(content):
    return httpx.MockTransport(lambda request: httpx.Response(200, json={"done": True, "message": {"content": json.dumps(content)}}))


def test_the_model_reads_text_only_and_answers_with_a_letter():
    # Neither frame names the person, so the model is asked; it never sees an image or gives coordinates.
    asked = page((60, 760, b"Mary Major declares the above."), (60, 680, b"Signature of the declarant:"), (60, 480, b"Signature of the witness:"),
                 boxes=framed(600, 700) + framed(400, 500))
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"done": True, "message": {"content": json.dumps({"place": "A"})}})
    place = choose_signature_place(asked, "Mary Major", transport=httpx.MockTransport(handler))
    assert place is not None and place.letter == "A"
    message = seen[0]["messages"][0]
    assert "images" not in message and "[A]" in message["content"] and "[B]" in message["content"]
    assert seen[0]["format"]["properties"]["place"]["enum"] == ["A", "B", "none"]
    assert choose_signature_place(asked, "Mary Major", transport=reply({"place": "none"})) is None
    with pytest.raises(ValueError):
        choose_signature_place(asked, "Mary Major", transport=reply({"place": "Z"}))


def test_the_model_cannot_take_a_place_from_the_frame_that_names_the_person():
    # Jane is named in both frames, so the code cannot choose alone; the model picks B, but only A's frame names her as signer.
    both = page((60, 720, b"Name: Jane Example"), (60, 650, b"Signature:"),
                (60, 520, b"Name: Jane Example"), (60, 450, b"Signature:"), boxes=framed(630, 760) + framed(430, 560))
    places = signature_places(both)
    assert place_by_name(places, "Jane Example") is None
    chosen = choose_signature_place(both, "Jane Example", transport=reply({"place": "B"}))
    assert chosen is not None and chosen.letter == "B"
    lone = page((60, 720, b"Name: Jane Example"), (60, 650, b"Signature:"), (60, 450, b"Signature of the witness:"),
                (60, 520, b"Name: Mary Major"), boxes=framed(630, 760) + framed(430, 560))
    assert choose_signature_place(lone, "Jane Example", transport=reply({"place": "B"})).letter == "A"


def test_roles_written_anywhere_decide_whose_labelled_place_it_is():
    roles = page((60, 760, b"Declarant: Mary Major"), (60, 740, b"Witness: John Example"), (60, 680, b"Signature of the declarant:"),
                 (60, 480, b"Signature of the witness:"), boxes=framed(600, 700) + framed(400, 500))
    places = signature_places(roles)
    assert place_by_name(places, "Mary Major").letter == "A" and place_by_name(places, "John Example").letter == "B"
    # Even when asked, a model that hands the declarant's place to the witness is not followed.
    unclear = page((60, 760, b"Witness: John Example"), (60, 680, b"Signature of the declarant:"), (60, 480, b"Signature:"),
                   boxes=framed(600, 700) + framed(400, 500))
    assert place_by_name(signature_places(unclear), "John Example") is None
    assert choose_signature_place(unclear, "John Example", transport=reply({"place": "A"})) is None


def test_a_page_without_text_is_left_to_the_picture_reading():
    with pytest.raises(NothingToChoose):
        choose_signature_place(page(lines=b"100 300 m 280 300 l S "), "Jane Example", transport=reply({"place": "A"}))


def test_room_at_the_end_of_the_last_page_avoids_what_is_written_there():
    crowded = page((72, 80, b"Footer text that fills the bottom left corner of the page"))
    spots = free_places_at_end(crowded, 2)
    assert len(spots) == 2 and all(spot[0] == 0 for spot in spots)
    assert all(box[1] > 95 or box[0] > 300 for _, box in spots)
