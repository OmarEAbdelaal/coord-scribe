from collections import Counter

import pytest

from coordy.redact import Alias, AliasError, Redactor, load_aliases


def make():
    return Redactor(
        [
            Alias("person", "Omar Elsayed", "P01", "Lead Architect", ["Omar", "عمر", "عمر السيد"]),
            Alias("org", "Nile Developments", "ORG01", "Client"),
        ]
    )


def test_full_name_wins_over_first_name():
    assert make().redact("Omar Elsayed and omar") == "P01 and P01"


def test_does_not_touch_parts_of_words():
    assert make().redact("Omari and Romar") == "Omari and Romar"


def test_arabic_names_with_attached_prefixes():
    r = make()
    assert r.redact("قال عمر") == "قال P01"
    assert r.redact("وعمر قال") == "وP01 قال"
    assert r.redact("لعمر السيد") == "لP01"
    assert r.redact("عمرو") == "عمرو"  # a different name


def test_emails_and_phones():
    r = make()
    assert r.redact("mail omar.e@example.com") == "mail [EMAIL]"
    assert r.redact("call +971 50 123 4567 or 01012345678") == "call [PHONE] or [PHONE]"
    assert r.redact("level 12, 1.5 m, 2026-10-28") == "level 12, 1.5 m, 2026-10-28"


def test_hits_and_unredact():
    r = make()
    hits = Counter()
    text = r.redact("Omar met Nile Developments", hits)
    assert hits == {"P01": 1, "ORG01": 1}
    assert r.unredact(text) == "Omar Elsayed met Nile Developments"
    assert r.unredact("P010 stays") == "P010 stays"


def test_guard_nested():
    assert make().guard({"a": ["Omar", {"b": "Nile Developments"}], "n": 3}) == {"a": ["P01", {"b": "ORG01"}], "n": 3}


def test_public_codes_have_no_names():
    assert "Omar" not in str(make().public_codes())


def test_load_aliases_validates(tmp_path):
    bad = tmp_path / "aliases.toml"
    bad.write_text('[[person]]\nname = "X"\ncode = "not a code"\n', encoding="utf-8")
    with pytest.raises(AliasError):
        load_aliases(bad)
    dup = tmp_path / "dup.toml"
    dup.write_text('[[person]]\nname = "A"\ncode = "P01"\n[[person]]\nname = "B"\ncode = "P01"\n', encoding="utf-8")
    with pytest.raises(AliasError):
        load_aliases(dup)


def test_default_template_loads(settings):
    from coordy.config import template_text

    settings.paths.aliases.write_text(template_text("aliases.toml"), encoding="utf-8")
    assert [a.code for a in load_aliases(settings.paths.aliases)] == ["P01"]
