from sealwharf.documents.names import contains_name, matches_name

IDENTITY = ("jane", "maria", "souza", "example")


def test_accepts_exact_name_and_initialed_middle_names():
    assert matches_name(IDENTITY, IDENTITY)
    assert matches_name(("jane", "m", "souza", "example"), IDENTITY)
    assert matches_name(("jane", "m", "s", "example"), IDENTITY)


def test_rejects_omitted_added_reordered_or_wrong_names():
    for words in [("jane", "example"), ("jane", "souza", "example"), ("jane", "m", "s", "example", "junior"),
                  ("jane", "s", "m", "example"), ("jane", "x", "souza", "example"), ("jane", "mar", "souza", "example")]:
        assert not matches_name(words, IDENTITY)


def test_first_and_last_names_are_never_abbreviated():
    assert not matches_name(("j", "maria", "souza", "example"), IDENTITY)
    assert not matches_name(("jane", "maria", "souza", "e"), IDENTITY)
    assert not matches_name(("jane",), ("jane",))


def test_finds_a_variant_inside_longer_text():
    assert contains_name(("signed", "by", "jane", "m", "s", "example", "today"), IDENTITY)
    assert not contains_name(("jane", "m", "example"), IDENTITY)


def test_names_compare_without_case_or_accents_in_both_directions():
    from sealwharf.documents.names import holds_name, name_words
    plain, accented = name_words("JOSE GONCALVES DA SILVA"), name_words("José Gonçalves da Silva")
    assert plain == accented == ("jose", "goncalves", "da", "silva")
    assert matches_name(name_words("jose gonçalves da silva"), plain) and holds_name(plain, name_words("José Gonçalves"))
