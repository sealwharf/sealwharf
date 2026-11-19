"""Compare normalized person names while tolerating abbreviated middle names.

Tokens are expected to be case-folded and stripped of accents by the caller.
The first and last names must appear in full and no name may be omitted or
added, so a match never relies on a partial identity.
"""

import re
import unicodedata


def name_words(value: str) -> tuple[str, ...]:
    """Split text into case-folded, accent-free words for name comparison."""
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return tuple(re.findall(r"[^\W_]+", "".join(c for c in normalized if not unicodedata.combining(c))))


def holds_name(subject: tuple[str, ...], identity: tuple[str, ...]) -> bool:
    """Report whether the identity appears as consecutive words of a certificate subject."""
    return bool(identity) and any(subject[i:i + len(identity)] == identity for i in range(len(subject) - len(identity) + 1))


def matches_name(words: tuple[str, ...], identity: tuple[str, ...]) -> bool:
    """Accept the exact name or one whose middle names are reduced to initials."""
    if len(identity) < 2 or len(words) != len(identity):
        return False
    if words[0] != identity[0] or words[-1] != identity[-1]:
        return False
    return all(word == name or (len(word) == 1 and name.startswith(word))
               for word, name in zip(words[1:-1], identity[1:-1], strict=True))


def contains_name(source: tuple[str, ...], identity: tuple[str, ...]) -> bool:
    """Report whether consecutive source words form the name or an accepted variant."""
    size = len(identity)
    return any(matches_name(source[index:index + size], identity) for index in range(len(source) - size + 1))


def only_in_running_text(text: str, identities: list[tuple[str, ...]]) -> bool:
    """Report whether a name is written, but only ever in the middle of a sentence.

    The person a document talks about is named inside a sentence; the person
    who signs it is named on a line of their own, or with a short label. A name
    counts as part of a sentence when its line has at least three words before
    it and either three after it or eight around it. One occurrence outside a
    sentence is enough for the answer to be False, so a doubt is never settled
    in favour of ignoring a signer.
    """
    tokens = [(word, number) for number, line in enumerate(text.splitlines()) for word in name_words(line)]
    words = tuple(word for word, _ in tokens)
    lines = [number for _, number in tokens]
    found = False
    for identity in identities:
        size = len(identity)
        for start in range(len(words) - size + 1):
            if not matches_name(words[start:start + size], identity):
                continue
            found = True
            end = start + size - 1
            before = sum(1 for number in lines[:start] if number == lines[start])
            after = sum(1 for number in lines[end + 1:] if number == lines[end])
            if before < 3 or (after < 3 and before + after < 8):
                return False
    return found
