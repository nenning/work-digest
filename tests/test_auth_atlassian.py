import base64
from unittest.mock import MagicMock
from digest.config import AtlassianConfig
from digest.auth.atlassian import get_auth_header, resolve_atlassian_config


def make_config(**overrides):
    defaults = dict(
        url="https://example.atlassian.net",
        email="user@example.com",
        api_token="secret123",
    )
    defaults.update(overrides)
    return AtlassianConfig(**defaults)


def test_returns_basic_header():
    header = get_auth_header(make_config())
    assert header.startswith("Basic ")


def test_encodes_correctly():
    header = get_auth_header(make_config())
    token = header[len("Basic "):]
    decoded = base64.b64decode(token).decode()
    assert decoded == "user@example.com:secret123"


def test_classic_config_defaults_api_bases_to_url():
    config = make_config()
    assert config.jira_api_base == "https://example.atlassian.net"
    assert config.confluence_api_base == "https://example.atlassian.net"


def test_scoped_returns_bearer_header():
    header = get_auth_header(make_config(auth_type="scoped", api_token="scoped-token-123"))
    assert header == "Bearer scoped-token-123"


def test_resolve_classic_config_is_unchanged_and_makes_no_network_call(mocker):
    mock_get = mocker.patch("digest.auth.atlassian.requests.get")
    config = make_config()
    resolved = resolve_atlassian_config(config)
    assert resolved is config
    mock_get.assert_not_called()


def test_resolve_scoped_config_uses_explicit_cloud_id_without_network_call(mocker):
    mock_get = mocker.patch("digest.auth.atlassian.requests.get")
    config = make_config(auth_type="scoped", cloud_id="explicit-cloud-id")
    resolved = resolve_atlassian_config(config)
    mock_get.assert_not_called()
    assert resolved.cloud_id == "explicit-cloud-id"
    assert resolved.jira_api_base == "https://api.atlassian.com/ex/jira/explicit-cloud-id"
    assert resolved.confluence_api_base == "https://api.atlassian.com/ex/confluence/explicit-cloud-id"


def test_resolve_scoped_config_fetches_cloud_id_from_tenant_info(mocker):
    mock_resp = MagicMock(ok=True)
    mock_resp.json.return_value = {"cloudId": "fetched-cloud-id"}
    mock_get = mocker.patch("digest.auth.atlassian.requests.get", return_value=mock_resp)
    config = make_config(auth_type="scoped")

    resolved = resolve_atlassian_config(config)

    mock_get.assert_called_once_with(
        "https://example.atlassian.net/_edge/tenant_info", timeout=10
    )
    assert resolved.cloud_id == "fetched-cloud-id"
    assert resolved.jira_api_base == "https://api.atlassian.com/ex/jira/fetched-cloud-id"
    assert resolved.confluence_api_base == "https://api.atlassian.com/ex/confluence/fetched-cloud-id"


def test_resolve_scoped_config_raises_clear_error_when_tenant_info_fails(mocker):
    mock_resp = MagicMock(ok=False, status_code=404)
    mocker.patch("digest.auth.atlassian.requests.get", return_value=mock_resp)
    config = make_config(auth_type="scoped")

    try:
        resolve_atlassian_config(config)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "cloud_id" in str(exc)
