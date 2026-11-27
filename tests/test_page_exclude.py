import re

import pytest

from digest.page_exclude import PageExclusion, load_exclusion


def test_excludes_by_id():
    ex = load_exclusion({"page_ids": ["123"]})
    assert ex.is_excluded("123", "Anything")
    assert not ex.is_excluded("124", "Anything")


def test_excludes_descendant_via_ancestor_id():
    ex = load_exclusion({"page_ids": [123]})
    assert ex.is_excluded("999", "Child", [{"id": "1", "title": "Root"}, {"id": "123", "title": "Parent"}])
    assert not ex.is_excluded("999", "Child", [{"id": "1", "title": "Root"}])


def test_excludes_by_title_pattern_case_insensitive():
    ex = load_exclusion({"title_patterns": [r"\bsandbox\b"]})
    assert ex.is_excluded("1", "Team SANDBOX notes")
    assert not ex.is_excluded("1", "Sandboxes")


def test_title_pattern_on_ancestor_excludes_subtree():
    ex = load_exclusion({"title_patterns": ["^archive"]})
    assert ex.is_excluded("5", "Plain page", [{"id": "2", "title": "Archive 2020"}])
    assert not ex.is_excluded("5", "Plain page", [{"id": "2", "title": "Current"}])


def test_empty_exclusion_is_falsy_and_excludes_nothing():
    for ex in (PageExclusion(), load_exclusion(None), load_exclusion({})):
        assert not ex
        assert not ex.is_excluded("1", "Title", [{"id": "2", "title": "Parent"}])
    assert load_exclusion({"page_ids": ["1"]})
    assert load_exclusion({"title_patterns": ["x"]})


def test_invalid_regex_names_the_config_key():
    with pytest.raises(ValueError, match="exclude_pages.title_patterns"):
        load_exclusion({"title_patterns": ["("]})


def test_ids_are_coerced_to_str():
    assert load_exclusion({"page_ids": [123, "456"]}).page_ids == frozenset({"123", "456"})
    assert isinstance(load_exclusion({"title_patterns": ["a"]}).title_patterns[0], re.Pattern)


@pytest.mark.parametrize("raw, key", [
    ({"title_patterns": "Sandbox"}, "exclude_pages.title_patterns"),
    ({"page_ids": "123456"}, "exclude_pages.page_ids"),
    ({"page_ids": 123456}, "exclude_pages.page_ids"),
    ([123], "exclude_pages"),
    ("x", "exclude_pages"),
])
def test_scalar_or_non_dict_values_rejected(raw, key):
    with pytest.raises(ValueError, match=key.replace(".", r"\.")):
        load_exclusion(raw)


def test_null_lists_tolerated():
    assert not load_exclusion({"page_ids": None, "title_patterns": None})
