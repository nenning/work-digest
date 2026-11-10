import pytest
from pathlib import Path
from digest.config import (
    AtlassianConfig,
    ProjectConfig,
    ProjectConfluenceConfig,
    ProjectJiraConfig,
    load_config,
)


VALID_YAML = """
atlassian:
  url: https://example.atlassian.net
  email: user@example.com
  api_token: tok123
  projects:
    - name: Project One
      jira:
        project: PROJ
      confluence:
        spaces: [ENG]
m365:
  tenant_id: abc-123
llm:
  provider: openai
  api_key: sk-test
  summary_models: gpt-4o
  endpoint: https://custom.endpoint/v1
schedule:
  times: ["08:00", "13:00"]
email:
  subject_prefix: "[Work]"
data_dir: ~/.digest
"""


def test_load_config(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(VALID_YAML)
    cfg = load_config(cfg_file)
    assert cfg.atlassian.url == "https://example.atlassian.net"
    assert len(cfg.atlassian.projects) == 1
    assert cfg.atlassian.projects[0].name == "Project One"
    assert cfg.atlassian.projects[0].jira.project == "PROJ"
    assert cfg.atlassian.projects[0].confluence.spaces == ["ENG"]
    assert cfg.atlassian.confluence_spaces == ["ENG"]
    assert cfg.m365.tenant_id == "abc-123"
    assert cfg.llm.endpoint == "https://custom.endpoint/v1"
    assert cfg.schedule.times == ["08:00", "13:00"]
    assert cfg.email.subject_prefix == "[Work]"


YAML_WITH_PROJECT_EXTRAS = """
atlassian:
  url: https://example.atlassian.net
  email: user@example.com
  api_token: tok123
  projects:
    - name: Project One
      jira:
        project: PROJ
        jql_extra: '"Team[Team]" = abc'
      confluence:
        spaces: [ENG]
      mgmt_summary:
        jira_jql_extra: 'statusCategory != Done'
        jira_board_id: 42
llm:
  provider: openai
  api_key: sk-test
  summary_models: gpt-4o
data_dir: ~/.digest
"""


def test_project_jql_extra_and_mgmt_summary_block(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(YAML_WITH_PROJECT_EXTRAS)
    cfg = load_config(cfg_file)
    project = cfg.atlassian.projects[0]
    assert project.jira.jql_extra == '"Team[Team]" = abc'
    assert project.mgmt_summary.jira_jql_extra == "statusCategory != Done"
    assert project.mgmt_summary.jira_board_id == 42


def test_project_without_mgmt_summary_block_is_none(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(VALID_YAML)
    cfg = load_config(cfg_file)
    assert cfg.atlassian.projects[0].mgmt_summary is None


def test_project_missing_name_raises(tmp_path):
    bad_yaml = VALID_YAML.replace("name: Project One", "not_name: Project One")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(bad_yaml)
    with pytest.raises(ValueError, match="name"):
        load_config(cfg_file)


def test_project_missing_jira_project_raises(tmp_path):
    bad_yaml = VALID_YAML.replace("        project: PROJ", "        not_project: PROJ")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(bad_yaml)
    with pytest.raises(ValueError, match="jira.project"):
        load_config(cfg_file)


def test_no_projects_defaults_to_empty_list(tmp_path):
    yaml_no_projects = VALID_YAML.replace(
        "  projects:\n    - name: Project One\n      jira:\n        project: PROJ\n      confluence:\n        spaces: [ENG]\n",
        "",
    )
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_no_projects)
    cfg = load_config(cfg_file)
    assert cfg.atlassian.projects == []
    assert cfg.atlassian.confluence_spaces == []


def test_m365_defaults_to_organizations(tmp_path):
    yaml_no_m365 = VALID_YAML.replace("m365:\n  tenant_id: abc-123\n", "")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_no_m365)
    cfg = load_config(cfg_file)
    assert cfg.m365.tenant_id == "organizations"


def test_missing_atlassian_raises(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("llm:\n  provider: openai\n  api_key: x\n  model: gpt-4o\n")
    with pytest.raises(KeyError):
        load_config(cfg_file)


def test_url_trailing_slash_stripped(tmp_path):
    yaml_with_slash = VALID_YAML.replace(
        "url: https://example.atlassian.net",
        "url: https://example.atlassian.net/"
    )
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_with_slash)
    cfg = load_config(cfg_file)
    assert not cfg.atlassian.url.endswith("/")


def test_data_dir_tilde_expanded(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(VALID_YAML)
    cfg = load_config(cfg_file)
    assert "~" not in str(cfg.data_dir)
    assert cfg.data_dir.is_absolute()


def test_m365_null_value_handled(tmp_path):
    """m365: with only comments parses as m365: null — must not crash."""
    yaml_m365_null = VALID_YAML.replace(
        "m365:\n  tenant_id: abc-123\n",
        "m365:\n"  # no children = null value
    )
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_m365_null)
    cfg = load_config(cfg_file)
    assert cfg.m365.tenant_id == "organizations"


def test_invalid_provider_raises(tmp_path):
    yaml_bad_provider = VALID_YAML.replace("provider: openai", "provider: gpt")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_bad_provider)
    with pytest.raises(ValueError, match="llm.provider"):
        load_config(cfg_file)


def test_empty_file_raises(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("")
    with pytest.raises(ValueError, match="empty or malformed"):
        load_config(cfg_file)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nonexistent.yaml")


# ---------------------------------------------------------------------------
# SmtpConfig
# ---------------------------------------------------------------------------

VALID_YAML_WITH_SMTP = VALID_YAML + """
smtp:
  host: smtp.office365.com
  port: 587
  username: user@company.com
  use_tls: true
  sender: noreply@company.com
"""


def test_smtp_config_loads(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(VALID_YAML_WITH_SMTP)
    cfg = load_config(cfg_file)
    assert cfg.smtp is not None
    assert cfg.smtp.host == "smtp.office365.com"
    assert cfg.smtp.port == 587
    assert cfg.smtp.username == "user@company.com"
    assert cfg.smtp.use_tls is True
    assert cfg.smtp.sender == "noreply@company.com"


def test_smtp_absent_gives_none(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(VALID_YAML)
    cfg = load_config(cfg_file)
    assert cfg.smtp is None


def test_smtp_defaults(tmp_path):
    yaml = VALID_YAML + "\nsmtp:\n  host: smtp.example.com\n  username: u@example.com\n"
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml)
    cfg = load_config(cfg_file)
    assert cfg.smtp.port == 587
    assert cfg.smtp.use_tls is True
    assert cfg.smtp.sender is None
    assert cfg.smtp.use_oauth2 is False


def test_smtp_use_oauth2_loads(tmp_path):
    yaml = VALID_YAML + "\nsmtp:\n  host: smtp.office365.com\n  username: u@example.com\n  use_oauth2: true\n"
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml)
    cfg = load_config(cfg_file)
    assert cfg.smtp.use_oauth2 is True


# ---------------------------------------------------------------------------
# AtlassianConfig.for_project / for_projects
# ---------------------------------------------------------------------------

def _make_atlassian(**overrides):
    defaults = dict(url="https://example.atlassian.net", email="u@e.com", api_token="tok")
    defaults.update(overrides)
    return AtlassianConfig(**defaults)


def _make_project(name="P1", jira_project="PROJ", spaces=None, url=None):
    return ProjectConfig(
        name=name,
        jira=ProjectJiraConfig(project=jira_project),
        confluence=ProjectConfluenceConfig(spaces=spaces or []),
        url=url,
    )


def test_confluence_spaces_unions_all_projects():
    config = _make_atlassian(projects=[
        _make_project("P1", "PROJ", spaces=["ENG"]),
        _make_project("P2", "OTHER", spaces=["DOC", "ENG"]),
    ])
    assert config.confluence_spaces == ["ENG", "DOC"]


def test_for_project_narrows_to_single_project():
    p1 = _make_project("P1", "PROJ", spaces=["ENG"])
    p2 = _make_project("P2", "OTHER", spaces=["DOC"])
    config = _make_atlassian(projects=[p1, p2])
    scoped = config.for_project(p1)
    assert scoped.projects == [p1]
    assert scoped.confluence_spaces == ["ENG"]
    assert scoped.url == config.url


def test_for_project_without_url_override_keeps_global_url():
    project = _make_project()
    config = _make_atlassian(projects=[project])
    scoped = config.for_project(project)
    assert scoped.url == "https://example.atlassian.net"
    assert scoped.jira_api_base == "https://example.atlassian.net"


def test_for_project_with_url_override_replaces_url_and_api_bases():
    project = _make_project(url="https://other-tenant.atlassian.net")
    config = _make_atlassian(projects=[project])
    scoped = config.for_project(project)
    assert scoped.url == "https://other-tenant.atlassian.net"
    assert scoped.jira_api_base == "https://other-tenant.atlassian.net"
    assert scoped.confluence_api_base == "https://other-tenant.atlassian.net"


def test_for_projects_groups_multiple_sharing_url():
    p1 = _make_project("P1", "PROJ", spaces=["ENG"])
    p2 = _make_project("P2", "OTHER", spaces=["DOC"])
    config = _make_atlassian(projects=[p1, p2])
    scoped = config.for_projects([p1, p2])
    assert scoped.confluence_spaces == ["ENG", "DOC"]
    assert scoped.url == config.url


# ---------------------------------------------------------------------------
# spec_monitor / gitlab
# ---------------------------------------------------------------------------

SPEC_MONITOR_YAML = """
spec_monitor:
  parent_page_id: 668860602
  selector:
    type: page_properties_id
    value: DETAILSPEC
"""

FULL_SPEC_MONITOR_YAML = """
spec_monitor:
  parent_page_id: "668860602"
  selector:
    type: label
    value: detailspec
  analysis_states: [Refined, Bereit]
  recipient: reviewer@example.com
  excluded_source_markers: ["Skizze"]
  retry:
    attempts: 5
    backoff_seconds: 0.5
  confluence_page:
    parent_page_id: 111222
"""

GITLAB_YAML = """
gitlab:
  url: https://gitlab.example.com/
  api_token: glpat-xyz
  default_project: Other
  repositories:
    - name: Diagramm-Repository
      project: group/diagrams
      default_branch: develop
      match: ["Diagramm-Repository"]
      docs_folder: "."
      documents:
        - match: "API-Konzept"
          path: docs/api-konzept.md
    - name: Other
      project: 42
"""


def _load(tmp_path, extra):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(VALID_YAML + extra)
    return load_config(cfg_file)


def test_spec_monitor_and_gitlab_absent_give_none(tmp_path):
    cfg = _load(tmp_path, "")
    assert cfg.spec_monitor is None
    assert cfg.gitlab is None


def test_spec_monitor_defaults(tmp_path):
    sm = _load(tmp_path, SPEC_MONITOR_YAML).spec_monitor
    assert sm.parent_page_id == "668860602"
    assert sm.selector_type == "page_properties_id"
    assert sm.selector_value == "DETAILSPEC"
    assert sm.analysis_states == ["Refined", "Abgenommen", "Angenommen"]
    assert sm.excluded_source_markers == ["Notizen", "Arbeitsnotizen", "Vorgängerfassung"]
    assert sm.retry.attempts == 3
    assert sm.retry.backoff_seconds == 2
    assert sm.recipient is None
    assert sm.publish_parent_page_id is None


def test_spec_monitor_defaults_are_not_shared_between_loads(tmp_path):
    first = _load(tmp_path, SPEC_MONITOR_YAML).spec_monitor
    first.analysis_states.append("Mutated")
    second = _load(tmp_path, SPEC_MONITOR_YAML).spec_monitor
    assert "Mutated" not in second.analysis_states


def test_spec_monitor_full_block(tmp_path):
    sm = _load(tmp_path, FULL_SPEC_MONITOR_YAML).spec_monitor
    assert sm.selector_type == "label"
    assert sm.selector_value == "detailspec"
    assert sm.analysis_states == ["Refined", "Bereit"]
    assert sm.recipient == "reviewer@example.com"
    assert sm.excluded_source_markers == ["Skizze"]
    assert sm.retry.attempts == 5
    assert sm.retry.backoff_seconds == 0.5
    assert sm.publish_parent_page_id == "111222"


def test_spec_monitor_empty_excluded_markers_disables_exclusion(tmp_path):
    yaml = SPEC_MONITOR_YAML + "  excluded_source_markers: []\n"
    assert _load(tmp_path, yaml).spec_monitor.excluded_source_markers == []


def test_spec_monitor_invalid_selector_type_raises(tmp_path):
    yaml = SPEC_MONITOR_YAML.replace("page_properties_id", "cql")
    with pytest.raises(ValueError, match="selector.type"):
        _load(tmp_path, yaml)


def test_spec_monitor_missing_selector_type_raises(tmp_path):
    yaml = "\nspec_monitor:\n  parent_page_id: 1\n  selector:\n    value: X\n"
    with pytest.raises(ValueError, match="selector.type"):
        _load(tmp_path, yaml)


def test_spec_monitor_missing_selector_value_raises(tmp_path):
    yaml = "\nspec_monitor:\n  parent_page_id: 1\n  selector:\n    type: label\n"
    with pytest.raises(ValueError, match="selector.value"):
        _load(tmp_path, yaml)


def test_spec_monitor_missing_parent_page_raises(tmp_path):
    yaml = "\nspec_monitor:\n  selector:\n    type: label\n    value: x\n"
    with pytest.raises(ValueError, match="parent_page_id"):
        _load(tmp_path, yaml)


def test_gitlab_block_loads(tmp_path):
    gl = _load(tmp_path, GITLAB_YAML).gitlab
    assert gl.url == "https://gitlab.example.com"
    assert gl.api_token == "glpat-xyz"
    assert gl.default_project == "Other"
    assert [r.name for r in gl.repositories] == ["Diagramm-Repository", "Other"]
    diagrams, other = gl.repositories
    assert diagrams.project == "group/diagrams"
    assert diagrams.default_branch == "develop"
    assert diagrams.match == ["Diagramm-Repository"]
    assert (diagrams.documents[0].match, diagrams.documents[0].path) == ("API-Konzept", "docs/api-konzept.md")
    assert other.project == "42"
    assert other.default_branch == "main"
    assert other.match == ["Other"] and other.documents == []
    assert diagrams.docs_folders == [""]
    assert other.docs_folders == [""]


def test_gitlab_without_repositories_loads(tmp_path):
    gl = _load(tmp_path, "\ngitlab:\n  url: https://g.example.com\n  api_token: t\n").gitlab
    assert gl.repositories == [] and gl.default_project is None


def test_gitlab_without_token_raises(tmp_path):
    with pytest.raises(ValueError, match="gitlab.url and gitlab.api_token"):
        _load(tmp_path, "\ngitlab:\n  url: https://g.example.com\n")


def test_gitlab_without_url_raises(tmp_path):
    with pytest.raises(ValueError, match="gitlab.url and gitlab.api_token"):
        _load(tmp_path, "\ngitlab:\n  api_token: t\n")


def test_gitlab_repository_without_project_raises(tmp_path):
    yaml = "\ngitlab:\n  url: https://g.example.com\n  api_token: t\n  repositories:\n    - name: X\n"
    with pytest.raises(ValueError, match="name and project"):
        _load(tmp_path, yaml)


def _gitlab(extra_repo, default="App"):
    head = "\ngitlab:\n  url: https://g.example.com\n  api_token: t\n"
    dflt = f"  default_project: {default}\n" if default else ""
    return head + dflt + "  repositories:\n" + extra_repo


def test_gitlab_docs_folder_forms(tmp_path):
    yaml = _gitlab(
        "    - name: App\n      project: grp/app\n      docs_folder: /docs/\n"
        "    - name: Multi\n      project: grp/multi\n      docs_folders: [docs, 'architecture/adr']\n"
        "    - name: Root\n      project: grp/root\n      docs_folder: .\n"
    )
    app, multi, root = _load(tmp_path, yaml).gitlab.repositories
    assert app.docs_folders == ["docs"]
    assert multi.docs_folders == ["docs", "architecture/adr"]
    assert root.docs_folders == [""]


def test_gitlab_default_project_may_be_the_project_path_and_resolves_to_the_name(tmp_path):
    yaml = _gitlab("    - name: App\n      project: grp/app\n", default="grp/app")
    assert _load(tmp_path, yaml).gitlab.default_project == "App"


def test_gitlab_default_project_must_be_a_configured_repository(tmp_path):
    yaml = _gitlab("    - name: App\n      project: grp/app\n", default="grp")
    with pytest.raises(ValueError, match="default_project must be the name"):
        _load(tmp_path, yaml)


def test_gitlab_project_must_be_full_path_or_numeric_id(tmp_path):
    yaml = _gitlab("    - name: App\n      project: just-a-name\n", default=None)
    with pytest.raises(ValueError, match="full 'group/project' path"):
        _load(tmp_path, yaml)


def test_spec_monitor_analyse_all_states_defaults_to_false(tmp_path):
    sm = _load(tmp_path, SPEC_MONITOR_YAML).spec_monitor
    assert sm.analyse_all_states is False
    assert sm.is_analysed("refined") and sm.is_analysed(" Angenommen ")
    assert not sm.is_analysed("In Ausarbeitung") and not sm.is_analysed("unknown")


def test_spec_monitor_ticket_block(tmp_path):
    sm = _load(tmp_path, SPEC_MONITOR_YAML + "  ticket:\n    project: PROJ\n    labels: [quellen]\n").spec_monitor
    assert sm.ticket.project == "PROJ"
    assert sm.ticket.issue_type == "Task" and sm.ticket.link_type == "Relates"
    assert sm.ticket.labels == ["quellen"]


def test_spec_monitor_ticket_defaults_to_none(tmp_path):
    assert _load(tmp_path, SPEC_MONITOR_YAML).spec_monitor.ticket is None


def test_spec_monitor_ticket_requires_project(tmp_path):
    with pytest.raises(ValueError, match="ticket.project is required"):
        _load(tmp_path, SPEC_MONITOR_YAML + "  ticket:\n    issue_type: Bug\n")


def test_effective_vision_llm_uses_the_standard_llm(tmp_path):
    from digest.config import effective_vision_llm
    cfg = _load(tmp_path, "")
    v = effective_vision_llm(cfg)
    assert (v.provider, v.api_key, v.endpoint) == (cfg.llm.provider, cfg.llm.api_key, cfg.llm.endpoint)
    assert v.model == cfg.llm.models[0]
    assert v.timeout >= 90


def test_spec_monitor_timeouts_and_workers_defaults(tmp_path):
    sm = _load(tmp_path, SPEC_MONITOR_YAML).spec_monitor
    assert sm.llm_timeout == 240 and sm.workers == 4
    assert sm.retry.timeout_seconds == 60


def test_spec_monitor_timeouts_and_workers_configurable(tmp_path):
    yaml = SPEC_MONITOR_YAML + "  llm_timeout: 400\n  workers: 8\n  retry:\n    timeout_seconds: 90\n"
    sm = _load(tmp_path, yaml).spec_monitor
    assert sm.llm_timeout == 400 and sm.workers == 8 and sm.retry.timeout_seconds == 90


@pytest.mark.parametrize("line", ["  llm_timeout: 0\n", "  workers: 0\n", "  retry:\n    timeout_seconds: 0\n"])
def test_spec_monitor_rejects_non_positive_values(tmp_path, line):
    with pytest.raises(ValueError, match="must be positive"):
        _load(tmp_path, SPEC_MONITOR_YAML + line)


def test_spec_llm_uses_the_spec_monitor_timeout(tmp_path):
    from digest.config import spec_llm
    cfg = _load(tmp_path, SPEC_MONITOR_YAML + "  llm_timeout: 333\n")
    assert spec_llm(cfg).llm_timeout == 333
    assert cfg.llm.llm_timeout != 333
    assert spec_llm(cfg).api_key == cfg.llm.api_key


def test_derived_vision_llm_uses_the_spec_monitor_timeout(tmp_path):
    from digest.config import effective_vision_llm
    cfg = _load(tmp_path, SPEC_MONITOR_YAML + "  llm_timeout: 333\n")
    assert effective_vision_llm(cfg).timeout == 333


def _load_llm_raw(tmp_path, summary_line):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(VALID_YAML.replace("  summary_models: gpt-4o\n", summary_line) + SPEC_MONITOR_YAML)
    return load_config(cfg_file)


def _load_llm(tmp_path, llm_extra, sm_extra=""):
    cfg_file = tmp_path / "config.yaml"
    yaml = VALID_YAML.replace("  endpoint: https://custom.endpoint/v1\n", "  endpoint: https://custom.endpoint/v1\n" + llm_extra)
    cfg_file.write_text(yaml + SPEC_MONITOR_YAML + sm_extra)
    return load_config(cfg_file)


def test_llm_model_lists_default(tmp_path):
    llm = _load(tmp_path, SPEC_MONITOR_YAML).llm
    assert llm.models == ["gpt-4o"] and llm.heavy_models == [] and llm.vision_model is None


def test_llm_summary_models_from_comma_separated_string_and_yaml_list(tmp_path):
    assert _load_llm(tmp_path, "", "").llm.models == ["gpt-4o"]
    assert _load_llm_raw(tmp_path, "  summary_models: small-a, small-b\n").llm.models == ["small-a", "small-b"]
    assert _load_llm_raw(tmp_path, "  summary_models: [small-a, small-b]\n").llm.models == ["small-a", "small-b"]


def test_llm_heavy_models_from_string_list_and_empty(tmp_path):
    assert _load_llm(tmp_path, "  heavy_models: big-a, big-b\n").llm.heavy_models == ["big-a", "big-b"]
    assert _load_llm(tmp_path, "  heavy_models: [big-a, big-b]\n").llm.heavy_models == ["big-a", "big-b"]
    assert _load_llm(tmp_path, "  heavy_models: ''\n").llm.heavy_models == []


@pytest.mark.parametrize("line", ["  heavy_models: [a, '']\n", "  heavy_models: [a, 3]\n", "  heavy_models: {a: b}\n"])
def test_llm_rejects_invalid_heavy_model_entries(tmp_path, line):
    with pytest.raises(ValueError, match="llm"):
        _load_llm(tmp_path, line)


def test_llm_summary_models_required(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(VALID_YAML.replace("  summary_models: gpt-4o\n", ""))
    with pytest.raises(ValueError, match="summary_models"):
        load_config(cfg_file)


@pytest.mark.parametrize("line,hint", [("model: gpt-4o", "summary_models"), ("models: x", "heavy_models"),
                                       ("fallback_model: x", "removed"), ("fallback_models: x", "removed")])
def test_llm_removed_or_renamed_keys_are_rejected(tmp_path, line, hint):
    with pytest.raises(ValueError, match=hint):
        _load_llm(tmp_path, "  " + line + "\n")


@pytest.mark.parametrize("key,new", [("models", "heavy_models"), ("summary_models", "summary_models"), ("vision_model", "vision_model")])
def test_spec_monitor_rejects_moved_model_keys(tmp_path, key, new):
    with pytest.raises(ValueError, match=f"llm.{new}"):
        _load(tmp_path, SPEC_MONITOR_YAML + f"  {key}: x\n")


def _cfg(tmp_path, llm_extra="", sm_extra=""):
    cfg = _load_llm(tmp_path, llm_extra, sm_extra)
    cfg.llm.models = ["std-1", "std-2"]
    return cfg


def test_spec_llm_uses_summary_models_when_heavy_is_unset(tmp_path):
    from digest.config import spec_llm
    cfg = _cfg(tmp_path)
    for purpose in ("heavy", "summary"):
        assert spec_llm(cfg, purpose).models == ["std-1", "std-2"]
    assert spec_llm(cfg).models == ["std-1", "std-2"]


def test_spec_llm_heavy_uses_heavy_models_and_summary_the_standard_ones(tmp_path):
    from digest.config import spec_llm
    cfg = _cfg(tmp_path, "  heavy_models: big\n", "  llm_timeout: 333\n")
    heavy, summary = spec_llm(cfg), spec_llm(cfg, "summary")
    assert heavy.models == ["big"] and spec_llm(cfg, "heavy").models == ["big"]
    assert summary.models == ["std-1", "std-2"]
    for l in (heavy, summary):
        assert l.llm_timeout == 333 and l.api_key == cfg.llm.api_key
    assert cfg.llm.models == ["std-1", "std-2"] and cfg.llm.llm_timeout != 333


def test_spec_llm_rejects_unknown_purpose(tmp_path):
    from digest.config import spec_llm
    with pytest.raises(ValueError, match="purpose"):
        spec_llm(_cfg(tmp_path), "bogus")


def test_spec_llm_without_spec_monitor_keeps_llm_timeout(tmp_path):
    from digest.config import spec_llm
    cfg = _load(tmp_path, "")
    assert spec_llm(cfg, "summary").models == cfg.llm.models
    assert spec_llm(cfg, "summary").llm_timeout == cfg.llm.llm_timeout


def test_heavy_llm_uses_heavy_models_else_summary_models(tmp_path):
    from digest.config import heavy_llm
    cfg = _cfg(tmp_path, "  heavy_models: big\n")
    h = heavy_llm(cfg.llm)
    assert h.models == ["big"] and h.llm_timeout == cfg.llm.llm_timeout
    assert heavy_llm(_cfg(tmp_path).llm).models == ["std-1", "std-2"]


def test_derived_vision_llm_uses_first_heavy_model(tmp_path):
    from digest.config import effective_vision_llm
    cfg = _cfg(tmp_path, "  heavy_models: [big-a, big-b]\n")
    assert effective_vision_llm(cfg).model == "big-a"


def test_derived_vision_llm_falls_back_to_first_summary_model(tmp_path):
    from digest.config import effective_vision_llm
    assert effective_vision_llm(_cfg(tmp_path)).model == "std-1"


def test_vision_model_overrides_heavy_models_for_vision_only(tmp_path):
    from digest.config import effective_vision_llm, spec_llm
    cfg = _cfg(tmp_path, "  heavy_models: [big-a, big-b]\n  vision_model: claude-opus-5-5\n")
    assert effective_vision_llm(cfg).model == "claude-opus-5-5"
    assert spec_llm(cfg).models == ["big-a", "big-b"]


def test_blank_vision_model_means_unset(tmp_path):
    assert _load_llm(tmp_path, "  vision_model: '  '\n").llm.vision_model is None


@pytest.mark.parametrize("line", ["  vision_model: [a, b]\n", "  vision_model: 3\n"])
def test_vision_model_must_be_a_single_name(tmp_path, line):
    with pytest.raises(ValueError, match="vision_model"):
        _load_llm(tmp_path, line)


def test_spec_monitor_ticket_proj_fields(tmp_path):
    yaml = (
        SPEC_MONITOR_YAML
        + "  ticket:\n    project: PROJ\n    issue_type: Story\n    labels: [cr]\n    priority: Medium\n"
        + "    team_id: 981844de-4593-40b3-aefe-c84d423990b8\n    parent: PROJ-12\n"
        + "    fields:\n      customfield_10091: 'AC1'\n    remote_link: false\n"
    )
    t = _load(tmp_path, yaml).spec_monitor.ticket
    assert (t.project, t.issue_type, t.labels, t.priority) == ("PROJ", "Story", ["cr"], "Medium")
    assert t.team_id == "981844de-4593-40b3-aefe-c84d423990b8" and t.team_field == "customfield_10001"
    assert t.parent == "PROJ-12" and t.fields == {"customfield_10091": "AC1"} and t.remote_link is False


def test_spec_monitor_ticket_new_fields_default(tmp_path):
    t = _load(tmp_path, SPEC_MONITOR_YAML + "  ticket:\n    project: PROJ\n").spec_monitor.ticket
    assert t.priority is None and t.team_id is None and t.parent is None
    assert t.fields == {} and t.remote_link is True
