from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Literal, Optional
import yaml

Provider = Literal["openai", "anthropic", "azure_openai"]
VALID_PROVIDERS = {"openai", "anthropic", "azure_openai"}


@dataclass
class AtlassianConfig:
    url: str
    email: str
    api_token: str
    jira_projects: List[str]
    confluence_spaces: List[str]
    jira_jql_extra: Optional[str] = None  # per AND an alle Jira-Queries angehängt


@dataclass
class M365Config:
    tenant_id: str = "organizations"
    client_id: Optional[str] = None   # custom Azure AD app client ID (required if tenant blocks Azure CLI)
    enabled: bool = True              # set False to skip Teams/Outlook and use local Outlook draft instead


@dataclass
class LLMConfig:
    provider: Provider
    api_key: str
    models: List[str]                                      # comma-separated in config
    endpoint: Optional[str] = None
    fallback_models: List[str] = field(default_factory=list)  # tried in order after primary models fail
    llm_workers: int = 4                                   # parallel LLM calls during summarization
    llm_timeout: int = 30                                  # seconds before a single LLM call is abandoned


@dataclass
class ScheduleConfig:
    times: List[str]  # ["08:00", "13:00"]


@dataclass
class EmailConfig:
    subject_prefix: str = "[Digest]"
    recipient: Optional[str] = None


@dataclass
class Config:
    atlassian: AtlassianConfig
    m365: M365Config
    llm: LLMConfig
    schedule: ScheduleConfig
    email: EmailConfig
    data_dir: Path
    language: str = "de"  # ISO 639-1 code; used for LLM output language


def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"Config file is empty or malformed: {path}")

    a = raw["atlassian"]
    m = raw.get("m365") or {}   # guard against `m365: null` (all children commented out)
    llm = raw["llm"]

    provider = llm["provider"]
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"llm.provider must be one of {sorted(VALID_PROVIDERS)}, got: {provider!r}")

    def _parse_models(value) -> List[str]:
        return [m.strip() for m in str(value).split(",") if m.strip()]

    fallback_raw = llm.get("fallback_models") or llm.get("fallback_model")

    return Config(
        atlassian=AtlassianConfig(
            url=a["url"].rstrip("/"),
            email=a["email"],
            api_token=a["api_token"],
            jira_projects=a.get("jira_projects", []),
            confluence_spaces=a.get("confluence_spaces", []),
            jira_jql_extra=a.get("jira_jql_extra"),
        ),
        m365=M365Config(
            tenant_id=m.get("tenant_id", "organizations"),
            client_id=m.get("client_id"),
            enabled=m.get("enabled", True),
        ),
        llm=LLMConfig(
            provider=provider,
            api_key=llm["api_key"],
            models=_parse_models(llm["model"]),
            endpoint=llm.get("endpoint"),
            fallback_models=_parse_models(fallback_raw) if fallback_raw else [],
            llm_workers=int(llm.get("llm_workers", 4)),
            llm_timeout=int(llm.get("llm_timeout", 30)),
        ),
        schedule=ScheduleConfig(times=raw.get("schedule", {}).get("times", ["08:00"])),
        email=EmailConfig(
            subject_prefix=raw.get("email", {}).get("subject_prefix", "[Digest]"),
            recipient=raw.get("email", {}).get("recipient") or None,
        ),
        data_dir=Path(raw.get("data_dir", "~/.digest")).expanduser(),
        language=raw.get("language", "de"),
    )
