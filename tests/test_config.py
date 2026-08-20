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
  model: gpt-4o
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
  model: gpt-4o
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
