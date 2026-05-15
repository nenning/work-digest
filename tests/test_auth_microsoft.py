import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from digest.auth.microsoft import get_token, SCOPES


def test_uses_cached_token(tmp_path):
    cache_file = tmp_path / "token_cache.bin"
    cache_file.write_text("{}")

    mock_app = MagicMock()
    mock_app.get_accounts.return_value = [{"username": "user@example.com"}]
    mock_app.acquire_token_silent.return_value = {"access_token": "cached_token_123"}

    with patch("digest.auth.microsoft.msal.PublicClientApplication", return_value=mock_app):
        token = get_token("organizations", cache_file)

    assert token == "cached_token_123"
    mock_app.acquire_token_silent.assert_called_once_with(SCOPES, account={"username": "user@example.com"})


def test_falls_back_to_device_flow(tmp_path):
    cache_file = tmp_path / "token_cache.bin"

    mock_app = MagicMock()
    mock_app.get_accounts.return_value = []
    mock_app.initiate_device_flow.return_value = {
        "user_code": "ABCDEF",
        "message": "Go to https://microsoft.com/devicelogin and enter ABCDEF",
    }
    mock_app.acquire_token_by_device_flow.return_value = {"access_token": "fresh_token"}

    with patch("digest.auth.microsoft.msal.PublicClientApplication", return_value=mock_app), \
         patch("digest.auth.microsoft.sys.stdin.isatty", return_value=True):
        token = get_token("organizations", cache_file)

    assert token == "fresh_token"


def test_raises_on_device_flow_failure(tmp_path):
    cache_file = tmp_path / "token_cache.bin"

    mock_app = MagicMock()
    mock_app.get_accounts.return_value = []
    mock_app.initiate_device_flow.return_value = {
        "error": "unauthorized_client",
        "error_description": "Tenant blocked app",
    }

    with patch("digest.auth.microsoft.msal.PublicClientApplication", return_value=mock_app), \
         patch("digest.auth.microsoft.sys.stdin.isatty", return_value=True):
        with pytest.raises(RuntimeError, match="IT"):
            get_token("organizations", cache_file)


def test_raises_in_non_interactive_session_when_no_cached_token(tmp_path):
    cache_file = tmp_path / "token_cache.bin"

    mock_app = MagicMock()
    mock_app.get_accounts.return_value = []

    with patch("digest.auth.microsoft.msal.PublicClientApplication", return_value=mock_app), \
         patch("digest.auth.microsoft.sys.stdin.isatty", return_value=False):
        with pytest.raises(RuntimeError, match="non-interactive"):
            get_token("organizations", cache_file)


def test_corrupt_cache_file_proceeds_to_fresh_auth(tmp_path):
    cache_file = tmp_path / "token_cache.bin"
    cache_file.write_text("not valid json {{{")

    mock_app = MagicMock()
    mock_app.get_accounts.return_value = []
    mock_app.initiate_device_flow.return_value = {
        "user_code": "XYZ",
        "message": "Go to ...",
    }
    mock_app.acquire_token_by_device_flow.return_value = {"access_token": "after_corrupt"}

    with patch("digest.auth.microsoft.msal.PublicClientApplication", return_value=mock_app), \
         patch("digest.auth.microsoft.sys.stdin.isatty", return_value=True):
        token = get_token("organizations", cache_file)

    assert token == "after_corrupt"
    assert not cache_file.exists() or cache_file.read_text() != "not valid json {{{"
