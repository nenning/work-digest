import base64
import dataclasses
import requests
from digest.config import AtlassianConfig

_TENANT_INFO_TIMEOUT = 10


def get_auth_header(config: AtlassianConfig) -> str:
    """Returns the Authorization header value for Atlassian REST API calls.

    Classic API tokens use 'Basic base64(email:token)'. Scoped API tokens
    (including those issued to service accounts) use 'Bearer <token>' and
    must be routed through api.atlassian.com -- see resolve_atlassian_config().
    """
    if config.auth_type == "scoped":
        return f"Bearer {config.api_token}"
    credentials = f"{config.email}:{config.api_token}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded}"


def resolve_atlassian_config(config: AtlassianConfig) -> AtlassianConfig:
    """Resolves the API base URLs a config's auth_type requires.

    Classic tokens talk to the tenant domain directly (config.url), which is
    already the default -- no network call needed. Scoped tokens must instead
    be routed through https://api.atlassian.com/ex/{jira,confluence}/{cloudId}/...,
    so this resolves the tenant's cloudId (via config.cloud_id if set, otherwise
    the tenant's public, unauthenticated /_edge/tenant_info endpoint) and returns
    an updated config pointing jira_api_base/confluence_api_base at those routes.
    """
    if config.auth_type != "scoped":
        return config

    cloud_id = config.cloud_id
    if not cloud_id:
        resp = requests.get(f"{config.url}/_edge/tenant_info", timeout=_TENANT_INFO_TIMEOUT)
        if not resp.ok:
            raise ValueError(
                f"Could not resolve Atlassian cloud_id from {config.url}/_edge/tenant_info "
                f"(HTTP {resp.status_code}). Set atlassian.cloud_id explicitly in config.yaml."
            )
        cloud_id = resp.json()["cloudId"]

    return dataclasses.replace(
        config,
        cloud_id=cloud_id,
        jira_api_base=f"https://api.atlassian.com/ex/jira/{cloud_id}",
        confluence_api_base=f"https://api.atlassian.com/ex/confluence/{cloud_id}",
    )
