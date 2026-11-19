"""Every language says and understands the same things; a new one only needs its own catalog file."""
import re

import pytest

from sealwharf.adapters.inbox import asks_verification, parse_command
from sealwharf.messages import (
    CATALOGS,
    DEFAULT_LANGUAGE,
    LANGUAGES,
    PHRASE_KINDS,
    Catalog,
    check_answers,
    check_catalog,
    read_catalog,
    resolve_language,
    text,
    wake_reply,
)

REFERENCE = CATALOGS[DEFAULT_LANGUAGE]


def test_english_portuguese_and_spanish_are_available():
    assert set(LANGUAGES) >= {"en", "pt-br", "es"}
    assert [CATALOGS[code].name for code in ("en", "pt-br", "es")] == ["English", "Português (Brasil)", "Español"]


@pytest.mark.parametrize("code", LANGUAGES)
def test_each_language_is_complete(code):
    catalog = CATALOGS[code]
    check_catalog(catalog, REFERENCE)
    assert set(catalog.phrases) == set(PHRASE_KINDS) and all(catalog.phrases.values())
    assert len(catalog.replies["wake"]) == len(set(catalog.replies["wake"])) == 25
    # Every message can be written out with its placeholders filled in.
    for key in catalog.messages:
        filled = text(code, key, **dict.fromkeys(
            ("fonts", "signed", "names", "minutes", "options", "all", "name", "owner", "file", "count", "files", "owners", "ahead",
             "requester", "waiting", "missing", "certificate", "date", "days", "moment", "position", "kind", "problem"), "x"))
        assert filled.strip() and "{" not in filled, key


@pytest.mark.parametrize("code", [code for code in LANGUAGES if code != DEFAULT_LANGUAGE])
def test_translations_are_not_left_in_english(code):
    # A message made only of placeholders, such as "{position}. {name} ({kind})", reads the same everywhere.
    same = [key for key, value in CATALOGS[code].messages.items()
            if value == REFERENCE.messages[key] and len(re.sub(r"\{\w+\}|\W", "", value)) > 8]
    assert same == []


def test_an_incomplete_language_is_refused():
    def catalog(**changes):
        return Catalog(**{"code": "xx", "name": "Test", "messages": dict(REFERENCE.messages), "replies": dict(REFERENCE.replies),
                          "phrases": dict(REFERENCE.phrases)} | changes)
    check_catalog(catalog(), REFERENCE)
    missing = dict(REFERENCE.messages)
    del missing["received"]
    broken = [
        catalog(messages=missing),
        catalog(messages=REFERENCE.messages | {"invented": "text"}),
        catalog(messages=REFERENCE.messages | {"signed_by": "Signed by nobody in particular"}),
        catalog(messages=REFERENCE.messages | {"signed_by": "{signed} and {someone}"}),
        catalog(messages=REFERENCE.messages | {"approved": " "}),
        catalog(phrases={kind: values for kind, values in REFERENCE.phrases.items() if kind != "wake"}),
        catalog(phrases=REFERENCE.phrases | {"yes": frozenset()}),
        catalog(replies={"wake": REFERENCE.replies["wake"][:3]}),
        catalog(replies={"wake": REFERENCE.replies["wake"][:24] + REFERENCE.replies["wake"][:1]}),
    ]
    for candidate in broken:
        with pytest.raises(RuntimeError, match="xx.toml is incomplete"):
            check_catalog(candidate, REFERENCE)
    with pytest.raises(RuntimeError, match="needs the sections"):
        read_catalog("xx", {"messages": {}})


def test_an_answer_word_cannot_mean_yes_in_one_language_and_no_in_another():
    check_answers(CATALOGS)
    clash = Catalog("xx", "Test", {}, {}, REFERENCE.phrases | {"no": frozenset({"sim"})})
    with pytest.raises(RuntimeError, match="two different answers"):
        check_answers(CATALOGS | {"xx": clash})


def test_language_codes_ignore_case_and_fall_back_from_region_to_language():
    assert resolve_language("PT-BR") == "pt-br" and resolve_language("pt_BR") == "pt-br" and resolve_language(" es ") == "es"
    assert resolve_language("es-MX") == "es" and resolve_language("en-US") == "en"
    for unknown in ("pt", "fr", "", "espanol", None, 5):
        assert resolve_language(unknown) is None
    # A language the bot does not have reads English.
    assert text("fr", "refused") == text("en", "refused") and wake_reply("fr") in REFERENCE.replies["wake"]
    assert text("es-AR", "refused") == "Rechazado. No se firmó nada."


def test_each_language_has_its_own_wake_phrase_and_all_of_them_work_for_everyone():
    for phrase in ("wake up bot", "WakeUpBot", "acorda bot", "ACORDABOT", "Despierta Bot", " despiertabot "):
        assert parse_command(phrase) == "ping", phrase
    # The message must be exactly the phrase: anything near it is ordinary conversation.
    for near in ("wake up", "wake up, bot", "wake up bot!", "wakeup bot", "despierta", "despierta el bot", "acorda o bot", "bot"):
        assert parse_command(near) is None, near


def test_the_status_command_is_written_in_english_by_everyone():
    for command in ("bot status", "Bot Status", " BOTSTATUS "):
        assert parse_command(command) == "status", command
    for near in ("status", "bot status?", "status bot", "bot  status", "bot estado", "status do bot", "the bot status"):
        assert parse_command(near) is None, near


def test_answers_and_the_verify_caption_are_understood_in_every_language():
    for word in ("yes", "Sim", "SÍ", "si", "approve", "aprovar", "aprobar", "vale", "ok", "Sim!"):
        assert parse_command(word) == "answer 1", word
    for word in ("no", "Não", "nao", "reject", "recusar", "rechazar", "cancel", "cancelar"):
        assert parse_command(word) == "answer 0", word
    for word in ("all", "todos", "Todas", "tudo", "todo"):
        assert parse_command(word) == "answer all", word
    assert parse_command("7") == "answer 7" and parse_command("si no") is None and parse_command("quizás") is None
    for caption in ("verify", "Verificar", "comprobar", " CHECK "):
        assert asks_verification(caption)
    assert not asks_verification("please verify") and not asks_verification(None)


def test_the_configuration_accepts_every_language_and_refuses_unknown_ones(tmp_path):
    from sealwharf.signer_config import load_config
    (tmp_path / "certificate.p12").write_bytes(b"synthetic")
    config = tmp_path / "sealwharf.yaml"
    body = ('language: {language}\ncontacts:\n  jane:\n    number: "5511999999999"\n  juan:\n    number: "5511988888888"\n    lang: {lang}\n'
            'certificates:\n  - id: one\n    name: Jane Example\n    file: certificate.p12\n    password: "synthetic"\n    owner: jane\n')
    config.write_text(body.format(language="pt-BR", lang="es-MX"), encoding="utf-8")
    assert load_config(config).languages == {"5511999999999": "pt-br", "5511988888888": "es"}
    for language, lang, message in (("fr", "es", "language must be one of"), ("en", "klingon", "lang of contact juan")):
        config.write_text(body.format(language=language, lang=lang), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_config(config)
