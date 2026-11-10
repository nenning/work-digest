import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
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
    models: List[str]                                      # llm.summary_models: per-item summaries (digest, spec monitor diffs)
    endpoint: Optional[str] = None
    llm_workers: int = 4                                   # parallel LLM calls during summarization
    llm_timeout: int = 30                                  # seconds before a single LLM call is abandoned
    heavy_models: List[str] = field(default_factory=list)  # llm.heavy_models: management summary + spec monitor heavy calls; empty = models
    vision_model: Optional[str] = None                     # llm.vision_model: draw.io image comparison; None = first heavy model


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
class RetryConfig:
    attempts: int = 3
    backoff_seconds: float = 2.0
    timeout_seconds: float = 60.0                  # per HTTP request (downloads get twice this)


@dataclass
class GitLabDocumentAlias:
    match: str
    path: str


@dataclass
class GitLabRepoConfig:
    name: str
    project: str                                   # "group/repo" path or numeric id
    default_branch: str = "main"
    match: List[str] = field(default_factory=list)  # text in a source row that selects this repo (default: [name])
    documents: List[GitLabDocumentAlias] = field(default_factory=list)   # optional explicit name -> path aliases
    docs_folders: List[str] = field(default_factory=list)  # folders searched for documents; "" = repository root


@dataclass
class GitLabConfig:
    url: str
    api_token: str
    default_project: Optional[str] = None          # repository name or project used for "im Repository"
    repositories: List[GitLabRepoConfig] = field(default_factory=list)


@dataclass
class TicketConfig:
    project: str                                   # Jira project key for --create-ticket
    issue_type: str = "Task"
    labels: List[str] = field(default_factory=list)
    link_type: str = "Relates"                     # link type to the Jira tickets named on the spec page
    priority: Optional[str] = None                 # priority name, e.g. "Medium"
    team_id: Optional[str] = None                  # raw team id string for the team custom field
    team_field: str = "customfield_10001"          # custom field id that holds the team
    parent: Optional[str] = None                   # issue key of a parent (e.g. a Feature)
    fields: Dict[str, Any] = field(default_factory=dict)   # extra raw fields, passed through as given
    remote_link: bool = True                       # add the Confluence page as a web link on the ticket


@dataclass
class VisionLLMConfig:
    """Separate image-capable LLM endpoint (draw.io comparison fallback)."""
    provider: Provider
    api_key: str
    model: str
    endpoint: Optional[str] = None
    timeout: int = 180


@dataclass
class SpecMonitorConfig:
    parent_page_id: str
    selector_type: str                             # "page_properties_id" | "label"
    selector_value: str
    analysis_states: List[str] = field(default_factory=lambda: ["Refined", "Abgenommen", "Angenommen"])
    recipient: Optional[str] = None
    excluded_source_markers: List[str] = field(default_factory=lambda: ["Notizen", "Arbeitsnotizen", "Vorgängerfassung"])
    retry: RetryConfig = field(default_factory=RetryConfig)
    publish_parent_page_id: Optional[str] = None   # confluence_page.parent_page_id
    ticket: Optional[TicketConfig] = None
    llm_timeout: int = 240                         # seconds per LLM call of the spec monitor (summaries, synthesis, rewrites)
    workers: int = 4                             # parallel source lookups / diff summaries per spec
    analyse_all_states: bool = False               # set by the --all-states CLI flag (not a yaml key)

    def is_analysed(self, status: str) -> bool:
        return self.analyse_all_states or status.strip().casefold() in {s.strip().casefold() for s in self.analysis_states}


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
    spec_monitor: Optional[SpecMonitorConfig] = None
    gitlab: Optional[GitLabConfig] = None


def heavy_llm(llm: LLMConfig) -> LLMConfig:
    """The llm with its heavy-call model list (llm.heavy_models) when configured, else the summary models.
    Used for everything of the management summary and the spec monitor's heavy calls."""
    return dataclasses.replace(llm, models=list(llm.heavy_models or llm.models))


def spec_llm(config: "Config", purpose: str = "heavy") -> LLMConfig:
    """The llm with the spec monitor's (longer) per-call timeout and the model list for the purpose
    ('heavy': llm.heavy_models, 'summary': llm.summary_models; heavy falls back to the summary models)."""
    if purpose not in ("heavy", "summary"):
        raise ValueError(f"spec_llm purpose must be 'heavy' or 'summary', got: {purpose!r}")
    llm = heavy_llm(config.llm) if purpose == "heavy" else config.llm
    sm = config.spec_monitor
    return dataclasses.replace(llm, llm_timeout=sm.llm_timeout if sm else llm.llm_timeout)


def effective_vision_llm(config: "Config") -> VisionLLMConfig:
    """Model used for the visual draw.io comparison: llm.vision_model when set, else the first heavy
    model (llm.heavy_models), else the first summary model (llm.summary_models). Provider, key and endpoint are
    always the standard llm's; the caller stops using it for the rest of the run as soon as it turns
    out not to accept images."""
    llm = config.llm
    return VisionLLMConfig(
        provider=llm.provider, api_key=llm.api_key,
        model=llm.vision_model or (llm.heavy_models or llm.models)[0],
        endpoint=llm.endpoint,
        timeout=config.spec_monitor.llm_timeout if config.spec_monitor else max(llm.llm_timeout * 3, 90),
    )


VALID_SELECTOR_TYPES = {"page_properties_id", "label"}


def _parse_model_list(value, key: str) -> List[str]:
    """Comma-separated string or YAML list of model names; empty/absent gives []."""
    if value is None:
        return []
    if isinstance(value, str):
        return [m.strip() for m in value.split(",") if m.strip()]
    if isinstance(value, list):
        if not all(isinstance(m, str) and m.strip() for m in value):
            raise ValueError(f"{key} entries must be non-empty strings, got: {value!r}")
        return [m.strip() for m in value]
    raise ValueError(f"{key} must be a comma-separated string or a list of model names, got: {value!r}")


def _load_spec_monitor(raw: Optional[dict]) -> Optional[SpecMonitorConfig]:
    if not raw:
        return None
    parent = raw.get("parent_page_id")
    sel = raw.get("selector") or {}
    sel_type, sel_value = sel.get("type"), sel.get("value")
    if not parent:
        raise ValueError("spec_monitor.parent_page_id is required")
    if sel_type not in VALID_SELECTOR_TYPES:
        raise ValueError(f"spec_monitor.selector.type must be one of {sorted(VALID_SELECTOR_TYPES)}, got: {sel_type!r}")
    if not sel_value:
        raise ValueError("spec_monitor.selector.value is required")
    retry_raw = raw.get("retry") or {}
    cfg = SpecMonitorConfig(
        parent_page_id=str(parent),
        selector_type=sel_type,
        selector_value=str(sel_value),
        recipient=raw.get("recipient") or None,
        retry=RetryConfig(
            attempts=int(retry_raw.get("attempts", 3)),
            backoff_seconds=float(retry_raw.get("backoff_seconds", 2)),
            timeout_seconds=float(retry_raw.get("timeout_seconds", 60)),
        ),
        publish_parent_page_id=(
            str((raw.get("confluence_page") or {}).get("parent_page_id"))
            if (raw.get("confluence_page") or {}).get("parent_page_id") else None
        ),
    )
    if raw.get("analysis_states"):
        cfg.analysis_states = [str(s) for s in raw["analysis_states"]]
    cfg.llm_timeout = int(raw.get("llm_timeout", 240))
    cfg.workers = int(raw.get("workers", 4))
    for moved, new in (("models", "heavy_models"), ("summary_models", "summary_models"), ("vision_model", "vision_model")):
        if moved in raw:
            raise ValueError(f"spec_monitor.{moved} moved to the llm section: use llm.{new}")
    if cfg.llm_timeout < 1 or cfg.workers < 1 or cfg.retry.timeout_seconds <= 0:
        raise ValueError("spec_monitor.llm_timeout, spec_monitor.workers and spec_monitor.retry.timeout_seconds must be positive")
    ticket_raw = raw.get("ticket")
    if ticket_raw:
        if not ticket_raw.get("project"):
            raise ValueError("spec_monitor.ticket.project is required when a ticket block is present")
        cfg.ticket = TicketConfig(
            project=str(ticket_raw["project"]),
            issue_type=str(ticket_raw.get("issue_type", "Task")),
            labels=[str(x) for x in (ticket_raw.get("labels") or [])],
            link_type=str(ticket_raw.get("link_type", "Relates")),
            priority=str(ticket_raw["priority"]) if ticket_raw.get("priority") else None,
            team_id=str(ticket_raw["team_id"]) if ticket_raw.get("team_id") else None,
            team_field=str(ticket_raw.get("team_field", "customfield_10001")),
            parent=str(ticket_raw["parent"]) if ticket_raw.get("parent") else None,
            fields=dict(ticket_raw.get("fields") or {}),
            remote_link=bool(ticket_raw.get("remote_link", True)),
        )
    if raw.get("excluded_source_markers") is not None:
        cfg.excluded_source_markers = [str(s) for s in raw["excluded_source_markers"]]
    return cfg


def _load_gitlab(raw: Optional[dict]) -> Optional[GitLabConfig]:
    if not raw:
        return None
    if not raw.get("url") or not raw.get("api_token"):
        raise ValueError("gitlab.url and gitlab.api_token are required when a gitlab block is present")
    repos = []
    for r in raw.get("repositories") or []:
        if not r.get("name") or not r.get("project"):
            raise ValueError("Each gitlab.repositories entry requires name and project")
        project = str(r["project"]).strip()
        if "/" not in project and not project.isdigit():
            raise ValueError(
                f"gitlab.repositories[{r['name']!r}].project must be the full 'group/project' path "
                f"or the numeric project id, got: {project!r}"
            )
        for d in r.get("documents") or []:
            if not d.get("match") or not d.get("path"):
                raise ValueError(f"Each gitlab.repositories[{r['name']!r}].documents entry requires match and path")
        folders_raw = r.get("docs_folders", r.get("docs_folder"))
        if folders_raw is None:
            folders = [""]
        elif isinstance(folders_raw, (list, tuple)):
            folders = list(folders_raw)
        else:
            folders = [folders_raw]
        folders = [("" if str(f).strip() in (".", "/") else str(f).strip().strip("/")) for f in folders]
        repos.append(GitLabRepoConfig(
            name=str(r["name"]),
            project=project,
            default_branch=r.get("default_branch", "main"),
            match=[str(m) for m in (r.get("match") or [str(r["name"])])],
            documents=[GitLabDocumentAlias(match=d["match"], path=d["path"]) for d in (r.get("documents") or [])],
            docs_folders=folders,
        ))
    default = raw.get("default_project") or None
    if default is not None:
        wanted = str(default).strip().casefold()
        hit = next((x for x in repos if wanted in (x.name.casefold(), x.project.casefold())), None)
        if hit is None:
            raise ValueError(
                f"gitlab.default_project must be the name (or project path) of a configured repository; "
                f"got {default!r}, configured: {[x.name for x in repos]}"
            )
        default = hit.name
    return GitLabConfig(
        url=str(raw["url"]).rstrip("/"),
        api_token=raw["api_token"],
        default_project=default,
        repositories=repos,
    )


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

    for removed, hint in (("model", "use llm.summary_models"), ("models", "renamed to llm.heavy_models"),
                          ("fallback_model", "removed"), ("fallback_models", "removed")):
        if removed in llm:
            raise ValueError(f"llm.{removed}: {hint}")
    if not llm.get("summary_models"):
        raise ValueError("llm.summary_models is required")
    vision_model = llm.get("vision_model")
    if vision_model is not None and not isinstance(vision_model, str):
        raise ValueError(f"llm.vision_model must be a single model name, got: {vision_model!r}")

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
            models=_parse_model_list(llm["summary_models"], "llm.summary_models"),
            endpoint=llm.get("endpoint"),
            llm_workers=int(llm.get("llm_workers", 4)),
            llm_timeout=int(llm.get("llm_timeout", 30)),
            heavy_models=_parse_model_list(llm.get("heavy_models"), "llm.heavy_models"),
            vision_model=(vision_model or "").strip() or None,
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
        spec_monitor=_load_spec_monitor(raw.get("spec_monitor")),
        gitlab=_load_gitlab(raw.get("gitlab")),
    )
