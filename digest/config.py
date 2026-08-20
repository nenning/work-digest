import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Literal, Optional
import yaml

Provider = Literal["openai", "anthropic", "azure_openai"]
VALID_PROVIDERS = {"openai", "anthropic", "azure_openai"}


AuthType = Literal["classic", "scoped"]
VALID_AUTH_TYPES = {"classic", "scoped"}


@dataclass
class ProjectJiraConfig:
    project: str
    jql_extra: Optional[str] = None  # per project -- AND'd onto this project's personal-digest queries


@dataclass
class ProjectConfluenceConfig:
    spaces: List[str] = field(default_factory=list)


@dataclass
class ProjectMgmtSummaryConfig:
    jira_jql_extra: Optional[str] = None  # AND'd onto "project = X" + the global mgmt_summary.jira_jql
    jira_board_id: Optional[int] = None   # required only for --sprint on this project


@dataclass
class ProjectConfig:
    name: str                                              # display label (mgmt-summary headings, logs)
    jira: ProjectJiraConfig
    confluence: ProjectConfluenceConfig = field(default_factory=ProjectConfluenceConfig)
    mgmt_summary: Optional[ProjectMgmtSummaryConfig] = None  # omit to exclude this project from --mgmt-summary
    url: Optional[str] = None                              # overrides atlassian.url for this project's requests only


@dataclass
class AtlassianConfig:
    url: str
    email: str
    api_token: str
    projects: List[ProjectConfig] = field(default_factory=list)
    auth_type: AuthType = "classic"       # "classic" = Basic email:token; "scoped" = Bearer token
    cloud_id: Optional[str] = None        # only used for auth_type "scoped"; auto-resolved if unset
    # Resolved API roots -- default to `url` (classic behavior). For "scoped" auth,
    # resolve_atlassian_config() overrides these to the api.atlassian.com/ex/{product}/{cloud_id}
    # routes that scoped tokens require. Human-facing links (browse/webui) always use `url`.
    jira_api_base: Optional[str] = None
    confluence_api_base: Optional[str] = None

    def __post_init__(self):
        if self.jira_api_base is None:
            self.jira_api_base = self.url
        if self.confluence_api_base is None:
            self.confluence_api_base = self.url

    @property
    def confluence_spaces(self) -> List[str]:
        """Union of every project's Confluence spaces, in first-seen order."""
        seen: List[str] = []
        for p in self.projects:
            for s in p.confluence.spaces:
                if s not in seen:
                    seen.append(s)
        return seen

    def for_project(self, project: ProjectConfig) -> "AtlassianConfig":
        """Return a copy of this config scoped to a single project. See for_projects()."""
        return self.for_projects([project])

    def for_projects(self, projects: List[ProjectConfig]) -> "AtlassianConfig":
        """Return a copy of this config scoped to a group of projects that share
        the same effective URL (e.g. via _group_by_url in confluence.py).

        `confluence_spaces` narrows to the union of just these projects' spaces.
        If (the first of) these projects overrides `url`, that URL becomes both
        the human-facing `url` and the API bases (mirroring how __post_init__
        derives the global api bases from `url` for classic auth) -- a project
        url override is not re-resolved through the scoped-auth cloud_id flow,
        so `auth_type: scoped` + a per-project url is a known unsupported
        combination rather than one silently getting the wrong cloud_id's routes.
        """
        project_url = projects[0].url if projects else None
        if project_url is None:
            return dataclasses.replace(self, projects=projects)
        url = project_url.rstrip("/")
        return dataclasses.replace(
            self, projects=projects, url=url, jira_api_base=url, confluence_api_base=url
        )


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
class SmtpConfig:
    host: str
    username: str
    port: int = 587
    use_tls: bool = True
    use_oauth2: bool = False
    sender: Optional[str] = None


@dataclass
class MgmtSummaryConfig:
    jira_jql: Optional[str] = None           # optional shared/base clause ("the top jql"), AND'd for every project
    ignore_users: List[str] = field(default_factory=list)   # display names to exclude -- global
    ignore_issue_types: List[str] = field(default_factory=list)  # issue types to skip -- global
    recipient: Optional[str] = None          # override send-to (empty = same as normal digest) -- global


@dataclass
class Config:
    atlassian: AtlassianConfig
    m365: M365Config
    llm: LLMConfig
    schedule: ScheduleConfig
    email: EmailConfig
    data_dir: Path
    language: str = "de"  # ISO 639-1 code; used for LLM output language
    mgmt_summary: MgmtSummaryConfig = field(default_factory=MgmtSummaryConfig)
    smtp: Optional[SmtpConfig] = None


def _load_smtp(raw: Optional[dict]) -> Optional[SmtpConfig]:
    if not raw:
        return None
    return SmtpConfig(
        host=raw["host"],
        username=raw["username"],
        port=int(raw.get("port", 587)),
        use_tls=bool(raw.get("use_tls", True)),
        use_oauth2=bool(raw.get("use_oauth2", False)),
        sender=raw.get("sender") or None,
    )


def _load_mgmt_summary(raw: Optional[dict]) -> MgmtSummaryConfig:
    if not raw:
        return MgmtSummaryConfig()
    return MgmtSummaryConfig(
        jira_jql=raw.get("jira_jql"),
        ignore_users=raw.get("ignore_users") or [],
        ignore_issue_types=raw.get("ignore_issue_types") or [],
        recipient=raw.get("recipient") or None,
    )


def _load_project(raw: dict) -> ProjectConfig:
    name = raw.get("name")
    if not name:
        raise ValueError("Each atlassian.projects entry requires a name")

    jira_raw = raw.get("jira") or {}
    jira_project = jira_raw.get("project")
    if not jira_project:
        raise ValueError(f"atlassian.projects[{name!r}].jira.project is required")

    confluence_raw = raw.get("confluence") or {}

    mgmt_raw = raw.get("mgmt_summary")
    mgmt_summary = None
    if mgmt_raw is not None:
        board_id = mgmt_raw.get("jira_board_id")
        mgmt_summary = ProjectMgmtSummaryConfig(
            jira_jql_extra=mgmt_raw.get("jira_jql_extra"),
            jira_board_id=int(board_id) if board_id is not None else None,
        )

    return ProjectConfig(
        name=name,
        jira=ProjectJiraConfig(
            project=jira_project,
            jql_extra=jira_raw.get("jql_extra"),
        ),
        confluence=ProjectConfluenceConfig(spaces=confluence_raw.get("spaces") or []),
        mgmt_summary=mgmt_summary,
        url=raw.get("url"),
    )


def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"Config file is empty or malformed: {path}")

    a = raw["atlassian"]
    m = raw.get("m365") or {}   # guard against `m365: null` (all children commented out)
    llm = raw["llm"]

    auth_type = a.get("auth_type", "classic")
    if auth_type not in VALID_AUTH_TYPES:
        raise ValueError(f"atlassian.auth_type must be one of {sorted(VALID_AUTH_TYPES)}, got: {auth_type!r}")

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
            projects=[_load_project(p) for p in (a.get("projects") or [])],
            auth_type=auth_type,
            cloud_id=a.get("cloud_id"),
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
        mgmt_summary=_load_mgmt_summary(raw.get("mgmt_summary")),
        smtp=_load_smtp(raw.get("smtp")),
    )
