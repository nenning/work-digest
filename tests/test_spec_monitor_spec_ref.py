import pytest

from digest.spec_monitor.spec_ref import UPDATE_ALL, normalize_spec_ref


@pytest.mark.parametrize("value,expected", [
    ("https://yourcompany.atlassian.net/wiki/spaces/RMEG/pages/678297650/API+CustomerManagement", "678297650"),
    ("https://yourcompany.atlassian.net/wiki/spaces/RMEG/pages/678297650", "678297650"),
    ("https://yourcompany.atlassian.net/wiki/pages/viewpage.action?pageId=678297650", "678297650"),
    ("  https://x.atlassian.net/wiki/spaces/A/pages/42/Title%20X?focusedCommentId=7  ", "42"),
    ("678297650", "678297650"),
    ("  API CustomerManagement ", "API CustomerManagement"),
])
def test_normalize(value, expected):
    assert normalize_spec_ref(value) == expected


def test_none_stays_none():
    assert normalize_spec_ref(None) is None


def test_url_without_page_id_is_an_error():
    with pytest.raises(ValueError, match="page id"):
        normalize_spec_ref("https://x.atlassian.net/wiki/x/AbCd")


def test_update_all_marker_is_not_a_valid_page_ref():
    assert UPDATE_ALL == "*"
    assert normalize_spec_ref(UPDATE_ALL) == "*"
