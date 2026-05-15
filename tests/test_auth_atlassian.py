import base64
from digest.config import AtlassianConfig
from digest.auth.atlassian import get_auth_header


def make_config():
    return AtlassianConfig(
        url="https://example.atlassian.net",
        email="user@example.com",
        api_token="secret123",
        jira_projects=[],
        confluence_spaces=[],
    )


def test_returns_basic_header():
    header = get_auth_header(make_config())
    assert header.startswith("Basic ")


def test_encodes_correctly():
    header = get_auth_header(make_config())
    token = header[len("Basic "):]
    decoded = base64.b64decode(token).decode()
    assert decoded == "user@example.com:secret123"
