import pytest
from pathlib import Path
from digest.config import load_config


VALID_YAML = """
atlassian:
  url: https://example.atlassian.net
  email: user@example.com
  api_token: tok123
  jira_projects: [PROJ]
  confluence_spaces: [ENG]
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
    assert cfg.atlassian.jira_projects == ["PROJ"]
    assert cfg.m365.tenant_id == "abc-123"
    assert cfg.llm.endpoint == "https://custom.endpoint/v1"
    assert cfg.schedule.times == ["08:00", "13:00"]
    assert cfg.email.subject_prefix == "[Work]"


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
