from datetime import datetime, timezone
from digest.state import load_state, save_state, get_last_run, EPOCH


def test_load_state_missing_file(tmp_path):
    result = load_state(tmp_path / "state.json")
    assert result == {}


def test_save_and_reload(tmp_path):
    f = tmp_path / "state.json"
    ts = datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc)
    save_state(f, {"jira": ts, "confluence": ts})
    loaded = load_state(f)
    assert loaded["jira"] == ts
    assert loaded["confluence"] == ts


def test_save_creates_parent_dirs(tmp_path):
    f = tmp_path / "nested" / "dir" / "state.json"
    ts = datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc)
    save_state(f, {"jira": ts})
    assert f.exists()


def test_save_leaves_no_tmp_file(tmp_path):
    f = tmp_path / "state.json"
    ts = datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc)
    save_state(f, {"jira": ts})
    assert not (tmp_path / "state.tmp").exists()
    assert f.exists()
    assert "jira" in f.read_text()


def test_load_state_corrupt_file(tmp_path):
    f = tmp_path / "state.json"
    f.write_text("not valid json")
    result = load_state(f)
    assert result == {}


def test_get_last_run_returns_epoch_for_unknown_source(tmp_path):
    state = {}
    assert get_last_run(state, "jira") == EPOCH


def test_get_last_run_returns_known_timestamp(tmp_path):
    ts = datetime(2026, 4, 9, 8, 0, 0, tzinfo=timezone.utc)
    state = {"jira": ts}
    assert get_last_run(state, "jira") == ts
