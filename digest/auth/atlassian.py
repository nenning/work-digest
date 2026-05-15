import base64
from digest.config import AtlassianConfig


def get_auth_header(config: AtlassianConfig) -> str:
    """Returns 'Basic <base64(email:token)>' for Atlassian REST API."""
    credentials = f"{config.email}:{config.api_token}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded}"
