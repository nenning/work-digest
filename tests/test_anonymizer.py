import random
import string
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from digest.anonymizer import Anonymizer


def test_round_trip_name_email_phone():
    a = Anonymizer(["Hans Müller"])
    text = "Hans Müller (hans@example.com, +41 79 123 45 67) approved it."
    anon = a.anonymize(text)
    assert "Hans" not in anon and "Müller" not in anon
    assert "hans@example.com" not in anon
    assert "123 45 67" not in anon
    assert "PERSON_001" in anon and "EMAIL_001" in anon and "PHONE_001" in anon
    assert a.restore(anon) == text


def test_reversed_and_comma_name_variants():
    a = Anonymizer(["Max Mustermann"])
    anon = a.anonymize("Max Mustermann, Mustermann Max and Mustermann, Max.")
    assert anon == "PERSON_001, PERSON_001 and PERSON_001."
    assert a.restore(anon) == "Max Mustermann, Max Mustermann and Max Mustermann."


def test_case_insensitive_and_nbsp():
    a = Anonymizer(["Hans Müller"])
    assert a.anonymize("by HANS MÜLLER today") == "by PERSON_001 today"
    assert a.anonymize("by hans müller today") == "by PERSON_001 today"
    assert a.anonymize("by Hans \n  Müller") == "by PERSON_001"


def test_genitive_replaced_and_restored():
    a = Anonymizer(["Hans Müller"])
    anon = a.anonymize("Hans Müllers Ticket")
    assert anon == "PERSON_001s Ticket"
    assert a.restore(anon) == "Hans Müllers Ticket"


def test_word_boundaries_respected():
    a = Anonymizer(["Hans Müller"])
    assert a.anonymize("XHans Müller") == "XHans Müller"
    assert a.anonymize("Hans Müllerei") == "Hans Müllerei"


def test_same_person_same_placeholder_two_people_two():
    a = Anonymizer(["Hans Müller", "Anna Meier"])
    anon = a.anonymize("Hans Müller, Anna Meier, Müller Hans")
    assert anon == "PERSON_001, PERSON_002, PERSON_001"
    assert a.anonymize("Anna Meier") == "PERSON_002"


def test_longest_name_wins():
    a = Anonymizer(["Hans Müller", "Hans Müller Meier"])
    anon = a.anonymize("Hans Müller Meier and Hans Müller")
    assert anon == "PERSON_001 and PERSON_002"
    assert a.restore(anon) == "Hans Müller Meier and Hans Müller"


def test_single_token_name_ignored_unless_allowed():
    a = Anonymizer(["Hans"])
    assert a.anonymize("Hans said hi") == "Hans said hi"
    a.add_names(["Zorro"], allow_single_token=True)
    assert a.anonymize("Zorro said hi") == "PERSON_001 said hi"
    a.add_names(["Hans"])
    assert a.anonymize("Hans said hi") == "Hans said hi"


def test_add_names_after_first_use():
    a = Anonymizer(["Hans Müller"])
    assert a.anonymize("Anna Meier") == "Anna Meier"
    a.add_names(["Anna Meier"])
    assert a.anonymize("Anna Meier and Hans Müller") == "PERSON_001 and PERSON_002"


def test_email_before_name():
    a = Anonymizer(["Hans Mueller"])
    anon = a.anonymize("Mail hans.mueller@x.ch or Hans Mueller")
    assert anon == "Mail EMAIL_001 or PERSON_001"
    assert a.restore(anon) == "Mail hans.mueller@x.ch or Hans Mueller"


def test_email_keyed_case_insensitive_restored_to_first_seen():
    a = Anonymizer()
    anon = a.anonymize("A.B@X.ch and a.b@x.ch")
    assert anon == "EMAIL_001 and EMAIL_001"
    assert a.restore(anon) == "A.B@X.ch and A.B@X.ch"


def test_phone_formats_matched():
    for phone in ["+41 79 123 45 67", "079 123 45 67", "044/123 45 67",
                  "+41 (0)79 123 45 67", "0041791234567"]:
        a = Anonymizer()
        anon = a.anonymize(f"call {phone} now")
        assert anon == "call PHONE_001 now", phone
        assert a.restore(anon) == f"call {phone} now"


def _digits(text):
    return "".join(ch for ch in text if ch.isdigit())


@pytest.mark.parametrize("text, kept", [
    ("079 123 45 67 044 987 65 43", []),
    ("079 123 45 67 - 044 987 65 43", []),
    ("079 123 45 67/044 987 65 43", []),
    ("27.11.2026 079 123 45 67", ["27.11.2026"]),
    ("079 123 45 67 27.11.2026", ["27.11.2026"]),
    ("079 123 45 67 2026-11-27", ["2026-11-27"]),
    ("Ticket 123456 079 123 45 67", ["Ticket"]),
])
def test_merged_phone_candidates_fail_closed(text, kept):
    a = Anonymizer()
    anon = a.anonymize(f"see {text} now")
    assert "1234567" not in _digits(anon) and "9876543" not in _digits(anon), anon
    for part in kept:
        assert part in anon
    assert a.restore(anon) == f"see {text} now"


@pytest.mark.parametrize("phone", [
    "030 1234-56-78", "+49 30 1234-56-78", "+41 44 1234-56-78",
    "+41 79 12.34.5678", "+33 1.23.45.6789",
])
def test_phone_with_date_shaped_group_masked(phone):
    a = Anonymizer()
    anon = a.anonymize(f"call {phone} now")
    assert anon == "call PHONE_001 now"
    assert a.restore(anon) == f"call {phone} now"


@pytest.mark.parametrize("text", [
    "PSSA-1234 and PSSA-5678", "timestamp 2026-11-27T12:30:45",
    "range 2026-11-27 - 2026-12-31", "version 1.2.3.4567 and 2026-11-27 12:30",
    "27.11.2026 10:50",
])
def test_dates_and_ids_untouched(text):
    assert Anonymizer().anonymize(text) == text


def test_two_phones_one_space_get_two_placeholders():
    a = Anonymizer()
    assert a.anonymize("079 123 45 67 044 987 65 43") == "PHONE_001 PHONE_002"


def test_dotted_capital_i_name_matched():
    a = Anonymizer(["İlker Yılmaz"])
    anon = a.anonymize("İlker Yılmaz said")
    assert anon == "PERSON_001 said"
    assert a.restore(anon) == "İlker Yılmaz said"


def test_three_token_rotated_variants():
    a = Anonymizer(["Anna Maria Meier"])
    anon = a.anonymize("Meier, Anna Maria; Meier Anna Maria; Meier Maria Anna; Anna Maria Meier")
    assert anon == "PERSON_001; PERSON_001; PERSON_001; PERSON_001"


def test_phone_plus_and_00_same_placeholder():
    a = Anonymizer()
    anon = a.anonymize("+41 79 123 45 67 / 0041791234567")
    assert anon == "PHONE_001 / PHONE_001"
    assert a.restore(anon) == "+41 79 123 45 67 / +41 79 123 45 67"


def test_adjacent_phones_both_replaced():
    a = Anonymizer()
    anon = a.anonymize("079 123 45 67 / 044 123 45 67")
    assert anon == "PHONE_001 / PHONE_002"
    assert a.restore(anon) == "079 123 45 67 / 044 123 45 67"


def test_phone_after_iso_date_replaced():
    a = Anonymizer()
    anon = a.anonymize("Called 2026-11-27 079 123 45 67")
    assert anon == "Called 2026-11-27 PHONE_001"


def test_long_digit_run_untouched():
    a = Anonymizer()
    assert a.anonymize("id 1234567890123456789") == "id 1234567890123456789"


def test_non_phone_numbers_untouched():
    a = Anonymizer()
    for text in ["PSSA-1234", "2026-11-27", "2026-11-27 10:50", "1.2.3.4", "12345678"]:
        assert a.anonymize(f"see {text} here") == f"see {text} here"


def test_restore_tolerates_case_and_inflection():
    a = Anonymizer(["Hans Müller"])
    a.anonymize("Hans Müller")
    assert a.restore("person_001 and PERSON_001s") == "Hans Müller and Hans Müllers"


def test_restore_leaves_unknown_numbers():
    a = Anonymizer(["Hans Müller"])
    a.anonymize("Hans Müller")
    assert a.restore("PERSON_002 EMAIL_001") == "PERSON_002 EMAIL_001"


def test_leftover_reports_unissued_placeholder():
    a = Anonymizer(["Hans Müller"])
    anon = a.anonymize("Hans Müller")
    assert a.leftover(anon) == []
    assert a.leftover(f"{anon} and PERSON_999") == ["PERSON_999"]


def test_thread_safe_same_placeholder_under_concurrency():
    a = Anonymizer(["Hans Müller"] + [f"First{i} Last{i}" for i in range(200)])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: a.anonymize("Hans Müller"), range(64)))
    assert len(set(results)) == 1
    assert a.restore(results[0]) == "Hans Müller"


def test_5000_names_50kb_prompt_fast():
    rng = random.Random(0)

    def word():
        return rng.choice(string.ascii_uppercase) + "".join(
            rng.choice(string.ascii_lowercase) for _ in range(rng.randint(3, 9)))

    names = list({f"{word()} {word()}" for _ in range(5000)})
    filler = [word() for _ in range(500)]
    parts, size = [], 0
    while size < 50_000:
        piece = rng.choice(names) if rng.random() < 0.05 else rng.choice(filler).lower()
        parts.append(piece)
        size += len(piece) + 1
    text = " ".join(parts)

    start = time.perf_counter()
    a = Anonymizer(names)
    anon = a.anonymize(text)
    elapsed = time.perf_counter() - start

    assert elapsed < 2.0, elapsed
    assert "PERSON_" in anon
    assert a.restore(anon) == text


@pytest.mark.parametrize("phone", [
    "079\xa0123\xa045\xa067", "+41\xa079 123 45 67", "+41\u202f79\u202f123\u202f45\u202f67",
    "079\u2013123\u201345\u201367",
])
def test_phone_with_nbsp_or_en_dash_masked(phone):
    a = Anonymizer()
    anon = a.anonymize(f"Kontakt: {phone} now")
    assert anon == "Kontakt: PHONE_001 now"
    assert a.restore(anon) == f"Kontakt: {phone} now"


def test_two_nbsp_phones_get_two_placeholders():
    a = Anonymizer()
    text = "079\xa0123\xa045\xa067\xa0044\xa0987\xa065\xa043"
    anon = a.anonymize(text)
    assert anon == "PHONE_001\xa0PHONE_002"
    assert a.restore(anon) == text


def test_date_next_to_nbsp_phone_kept():
    a = Anonymizer()
    text = "27.11.2026\xa0079\xa0123\xa045\xa067"
    anon = a.anonymize(text)
    assert anon == "27.11.2026\xa0PHONE_001"
    assert a.restore(anon) == text


def test_nbsp_phone_from_confluence_storage():
    from digest.sources.confluence import _storage_to_text
    a = Anonymizer()
    anon = a.anonymize(_storage_to_text("<p>Kontakt: 079&nbsp;123&nbsp;45&nbsp;67</p>"))
    assert "123" not in anon and "PHONE_001" in anon
