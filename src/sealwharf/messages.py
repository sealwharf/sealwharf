"""Everything the bot says or understands in a human language, chosen per phone number.

Each language is one catalog file in `locales/`, named after its code (`en`,
`pt-br`, `es`): the messages, the replies to the wake phrase, and the phrases
people may write to the bot. `en.toml` is the reference; a catalog that lacks
an entry, has an extra one or uses other {placeholders} stops the program at
start, so a language is either complete or absent. Adding a language is adding
one file.

The tone is that of a helpful person: short, friendly, and free of technical
detail the reader cannot act on.
"""

import random
import string
import tomllib
import unicodedata
from dataclasses import dataclass
from importlib.resources import files

DEFAULT_LANGUAGE = "en"
# What people may write to the bot. The answers must never be confused with one another.
PHRASE_KINDS = ("wake", "yes", "no", "all", "verify")
ANSWER_KINDS = ("yes", "no", "all")


@dataclass(frozen=True)
class Catalog:
    code: str
    name: str
    messages: dict[str, str]
    replies: dict[str, tuple[str, ...]]
    phrases: dict[str, frozenset[str]]


def normalized(value: str) -> str:
    """Lower case, without accents and outer spaces: how written phrases are compared."""
    plain = unicodedata.normalize("NFKD", value.strip().casefold())
    return "".join(character for character in plain if not unicodedata.combining(character))


def _fields(message: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(message) if name}


def read_catalog(code: str, data: dict) -> Catalog:
    try:
        messages = {key: str(value) for key, value in data["messages"].items()}
        replies = {kind: tuple(str(value) for value in values) for kind, values in data["replies"].items()}
        phrases = {kind: frozenset(normalized(str(value)) for value in values) for kind, values in data["phrases"].items()}
        return Catalog(code, str(data["language"]["name"]), messages, replies, phrases)
    except (KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"Language file {code}.toml needs the sections language, phrases, replies and messages.") from exc


def check_catalog(catalog: Catalog, reference: Catalog) -> None:
    """Refuse a catalog that does not say everything the reference says, in the same shape."""
    problems = [f"missing message {key}" for key in reference.messages.keys() - catalog.messages.keys()]
    problems += [f"unknown message {key}" for key in catalog.messages.keys() - reference.messages.keys()]
    problems += [f"message {key} must use the placeholders {sorted(_fields(reference.messages[key]))}"
                 for key in reference.messages.keys() & catalog.messages.keys()
                 if _fields(catalog.messages[key]) != _fields(reference.messages[key])]
    problems += [f"empty message {key}" for key, value in catalog.messages.items() if not value.strip()]
    if set(catalog.phrases) != set(PHRASE_KINDS) or not all(catalog.phrases.values()):
        problems.append("phrases must list at least one entry for each of: " + ", ".join(PHRASE_KINDS))
    if set(catalog.replies) != set(reference.replies) or any(
            len(catalog.replies.get(kind, ())) != len(replies) or len(set(catalog.replies.get(kind, ()))) != len(replies)
            for kind, replies in reference.replies.items()):
        problems.append("replies must have as many different entries as the reference catalog")
    if problems:
        raise RuntimeError(f"Language file {catalog.code}.toml is incomplete: " + "; ".join(sorted(problems)) + ".")


def check_answers(catalogs: dict[str, Catalog]) -> None:
    """People answer in whatever language they like, so a word must mean the same answer in all of them."""
    meanings: dict[str, str] = {}
    for catalog in catalogs.values():
        for kind in ANSWER_KINDS:
            for phrase in catalog.phrases[kind]:
                if meanings.setdefault(phrase, kind) != kind:
                    raise RuntimeError(f"The answer word '{phrase}' means two different answers across the language files.")


def _load() -> dict[str, Catalog]:
    catalogs = {}
    for entry in sorted((files("sealwharf") / "locales").iterdir(), key=lambda item: item.name):
        if entry.name.endswith(".toml"):
            code = entry.name.removesuffix(".toml")
            catalogs[code] = read_catalog(code, tomllib.loads(entry.read_text(encoding="utf-8")))
    if DEFAULT_LANGUAGE not in catalogs:
        raise RuntimeError(f"The reference language file {DEFAULT_LANGUAGE}.toml is missing.")
    for catalog in catalogs.values():
        check_catalog(catalog, catalogs[DEFAULT_LANGUAGE])
    check_answers(catalogs)
    return catalogs


CATALOGS = _load()
LANGUAGES = tuple(CATALOGS)
PHRASES = {kind: frozenset().union(*(catalog.phrases[kind] for catalog in CATALOGS.values())) for kind in PHRASE_KINDS}


def resolve_language(code: object) -> str | None:
    """Return the catalog for a language code, or None when there is none.

    Codes are compared without regard to case or to `_` versus `-`, and a
    regional code falls back to its language: `es-MX` reads the `es` catalog.
    """
    if not isinstance(code, str):
        return None
    parts = code.strip().casefold().replace("_", "-").split("-")
    for length in range(len(parts), 0, -1):
        candidate = "-".join(parts[:length])
        if candidate in CATALOGS:
            return candidate
    return None


def _catalog(language: str) -> Catalog:
    return CATALOGS[resolve_language(language) or DEFAULT_LANGUAGE]


def phrase_kind(written: object, kinds: tuple[str, ...] = PHRASE_KINDS) -> str | None:
    """Tell which known phrase a whole message is, in any supported language, or None."""
    if not isinstance(written, str):
        return None
    plain = normalized(written)
    return next((kind for kind in kinds if plain in PHRASES[kind]), None)


def wake_reply(language: str) -> str:
    """Return one of the answers to the wake phrase, in the requested language."""
    return random.choice(_catalog(language).replies["wake"])


def text(language: str, key: str, **values: object) -> str:
    """Return one message in the requested language, falling back to English."""
    return _catalog(language).messages[key].format(**values)
