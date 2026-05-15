import os
import sys
import msal
from pathlib import Path

AZURE_CLI_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
SCOPES = [
    "Mail.Read",
    "Mail.Send",
    "Chat.Read",
    "ChannelMessage.Read.All",
    "User.Read",
]


def get_token(tenant_id: str, cache_file: Path, client_id: str | None = None) -> str:
    """Returns a valid access token. Refreshes silently or prompts device code.

    client_id: your custom Azure AD app's Application ID. Falls back to the Azure CLI
    public client ID if not provided, but some corporate tenants block that ID —
    in that case register your own app and pass its client_id via config.yaml.
    """
    effective_client_id = client_id or AZURE_CLI_CLIENT_ID
    cache = msal.SerializableTokenCache()
    if cache_file.exists():
        try:
            cache.deserialize(cache_file.read_text())
        except Exception:
            cache_file.unlink(missing_ok=True)  # evict corrupt cache, proceed fresh

    app = msal.PublicClientApplication(
        effective_client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache, cache_file)
            return result["access_token"]
        # Silent refresh failed (token revoked, policy change, etc.) — fall through to device flow

    if not sys.stdin.isatty():
        raise RuntimeError(
            "M365 token has expired and cannot be refreshed in a non-interactive session. "
            "Run 'python main.py --setup-auth' manually to refresh your credentials."
        )

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(
            f"M365 auth failed to start device flow: {flow.get('error_description')}\n"
            "If your tenant blocks this, ask IT to register an Azure AD app with these "
            "delegated scopes: Mail.Read, Mail.Send, Chat.Read, ChannelMessage.Read.All, User.Read"
        )

    print(flow["message"])
    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        description = result.get("error_description") or result.get("error") or str(result)
        raise RuntimeError(f"M365 auth failed: {description}")

    _save_cache(cache, cache_file)
    return result["access_token"]


def _save_cache(cache: msal.SerializableTokenCache, cache_file: Path) -> None:
    if not cache.has_state_changed:
        return
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(".tmp")
    # Write to a temp file first so a crash mid-write doesn't corrupt the cache.
    # Restrictive permissions — cache contains refresh tokens.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(cache.serialize())
    tmp.replace(cache_file)
