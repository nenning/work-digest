import subprocess
from unittest.mock import MagicMock

import pytest

from digest.config import GitLabConfig, GitLabRepoConfig, RetryConfig
from digest.spec_monitor import trace_code
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import TraceSourceError
from digest.spec_monitor.trace_code import GitLabTrace, search_code, search_local

RETRY = RetryConfig()
GL = GitLabConfig(url="https://gitlab.example.com", api_token="tok")
REPO = GitLabRepoConfig(name="application", project="grp/app", default_branch="main")
API = "https://gitlab.example.com/api/v4/projects/grp%2Fapp"


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def resp(status=200, data=None):
    r = MagicMock()
    r.status_code = status
    r.ok = status < 400
    r.json.return_value = data
    return r


class FakeApi:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for suffix, value in self.routes.items():
            if url.endswith(suffix):
                if isinstance(value, Exception):
                    raise value
                return value
        return resp(200, [])


@pytest.fixture
def api(mocker):
    fake = FakeApi()
    mocker.patch.object(trace_code, "request_with_retry", side_effect=fake)
    return fake


@pytest.fixture(autouse=True)
def no_git(mocker):
    mocker.patch.object(trace_code.subprocess, "run", side_effect=FileNotFoundError("git"))


def test_local_search_ranks_by_distinct_terms(tmp_path):
    write(tmp_path, "src/a.py", "retry policy\nbackoff here\nunrelated\n")
    write(tmp_path, "src/b.py", "only retry\n")
    found, _ = search_local(tmp_path, ["retry", "backoff"])
    assert [c.ref for c in found] == ["src/a.py", "src/b.py"]
    assert found[0].kind == "code"
    assert len(found[0].detail.splitlines()) <= 3 and "retry policy" in found[0].detail


def test_local_search_detail_is_capped_at_three_lines(tmp_path):
    write(tmp_path, "x.txt", "\n".join(f"retry {i}" for i in range(10)))
    found, _ = search_local(tmp_path, ["retry"])
    assert len(found[0].detail.splitlines()) == 3


def test_local_search_is_case_insensitive(tmp_path):
    write(tmp_path, "x.txt", "BatchProcessing starts\n")
    assert [c.ref for c in search_local(tmp_path, ["batchprocessing"])[0]] == ["x.txt"]


def test_local_search_skips_ignored_binary_and_huge_files(tmp_path):
    write(tmp_path, "node_modules/x.js", "retry")
    write(tmp_path, ".git/config", "retry")
    (tmp_path / "bin.dat").write_bytes(b"retry\x00\x01")
    write(tmp_path, "huge.txt", "retry" + "x" * (2 * 1024 * 1024))
    write(tmp_path, "ok.txt", "retry")
    assert [c.ref for c in search_local(tmp_path, ["retry"])[0]] == ["ok.txt"]


def test_unreadable_file_is_counted_not_fatal(tmp_path, mocker):
    write(tmp_path, "a.txt", "retry")
    write(tmp_path, "b.txt", "retry")
    real_open = open

    def flaky(path, *a, **kw):
        if str(path).endswith("a.txt"):
            raise PermissionError("denied")
        return real_open(path, *a, **kw)

    mocker.patch("builtins.open", side_effect=flaky)
    found, note = search_local(tmp_path, ["retry"])
    assert [c.ref for c in found] == ["b.txt"]
    assert "1 file(s) unreadable" in note


def test_file_cap_is_reported(tmp_path, mocker):
    mocker.patch.object(trace_code, "MAX_FILES", 2)
    for i in range(5):
        write(tmp_path, f"f{i}.txt", "retry")
    found, note = search_local(tmp_path, ["retry"])
    assert len(found) <= 2 and "stopped after 2 files" in note


def test_missing_root_raises(tmp_path):
    with pytest.raises(TraceSourceError, match="not found"):
        search_local(tmp_path / "nope", ["x"])


def test_checkout_note_for_non_git_folder(tmp_path):
    write(tmp_path, "a.txt", "retry")
    assert "no git repository" in search_local(tmp_path, ["retry"])[1]


def test_checkout_note_names_branch_and_last_commit(tmp_path, mocker):
    write(tmp_path, "a.txt", "retry")

    def run(cmd, **kw):
        out = {"log": "2026-11-10T09:00:00+01:00\n", "rev-parse": "feature/x\n"}[cmd[3]]
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    mocker.patch.object(trace_code.subprocess, "run", side_effect=run)
    note = search_local(tmp_path, ["retry"])[1]
    assert "feature/x" in note and "2026-11-10" in note


def test_merge_requests_and_branches(api):
    api.routes = {
        "/merge_requests": resp(200, [{"iid": 7, "title": "Retry 5", "web_url": "https://gl/mr/7", "source_branch": "feature/retry"}]),
        "/repository/branches": resp(200, [{"name": "feature/retry"}, {"name": "main"}]),
    }
    t = GitLabTrace(GL, RETRY)
    mrs = t.merge_requests(REPO, ["retry", "retry again"])
    assert [(c.kind, c.ref, c.url) for c in mrs] == [("mr", "!7", "https://gl/mr/7")]
    branches = t.branches(REPO, ["retry"])
    assert [(c.kind, c.ref) for c in branches] == [("branch", "feature/retry")]
    mr_calls = [c for c in api.calls if c[1].endswith("/merge_requests")]
    assert mr_calls[0][2]["params"]["state"] == "opened" and mr_calls[0][2]["params"]["search"] == "retry"
    assert mr_calls[0][2]["headers"]["PRIVATE-TOKEN"] == "tok"
    assert mr_calls[0][1] == f"{API}/merge_requests"


def test_blobs_use_the_default_branch(api):
    api.routes = {"/search": resp(200, [{"path": "src/Retry.java", "startline": 3, "data": "retry = 5"}])}
    found = GitLabTrace(GL, RETRY).blobs(REPO, ["retry"])
    assert [(c.kind, c.ref) for c in found] == [("code", "src/Retry.java")]
    params = api.calls[0][2]["params"]
    assert params["scope"] == "blobs" and params["ref"] == "main" and params["search"] == "retry"


def test_gitlab_non_ok_raises_trace_source_error(api):
    api.routes = {"/merge_requests": resp(403)}
    with pytest.raises(TraceSourceError, match=r"application.*403"):
        GitLabTrace(GL, RETRY).merge_requests(REPO, ["x"])


def test_gitlab_transport_failure_raises_trace_source_error(api):
    api.routes = {"/merge_requests": RequestFailed("u", 3, "timeout")}
    with pytest.raises(TraceSourceError, match="timeout"):
        GitLabTrace(GL, RETRY).merge_requests(REPO, ["x"])


def test_search_code_local_folder_plus_api(tmp_path, api):
    write(tmp_path, "a.txt", "retry")
    repo = GitLabRepoConfig(name="application", project="grp/app", local_path=tmp_path)
    api.routes = {
        "/merge_requests": resp(200, [{"iid": 1, "title": "Retry", "web_url": "u"}]),
        "/repository/branches": resp(200, []),
    }
    result = search_code(GL, [repo], RETRY, ["retry"])
    assert {c.kind for c in result.candidates} == {"code", "mr"} and not result.problems
    assert not any(c[1].endswith("/search") for c in api.calls)
    text = " | ".join(result.searched)
    assert "application" in text and str(tmp_path) in text and "merge requests" in text


def test_missing_local_folder_falls_back_to_blob_search_with_problem(tmp_path, api):
    repo = GitLabRepoConfig(name="application", project="grp/app", local_path=tmp_path / "gone")
    api.routes = {"/search": resp(200, [{"path": "x.py"}])}
    result = search_code(GL, [repo], RETRY, ["retry"])
    assert any("not found" in p and "GitLab search used instead" in p for p in result.problems)
    assert [c.ref for c in result.candidates if c.kind == "code"] == ["x.py"]


def test_without_local_path_blob_search_replaces_it(api):
    api.routes = {"/search": resp(200, [{"path": "x.py"}])}
    result = search_code(GL, [REPO], RETRY, ["retry"])
    assert not result.problems
    assert any("GitLab blob search" in s and "default branch" in s for s in result.searched)


def test_gitlab_failure_is_one_problem_other_sources_continue(tmp_path, api):
    write(tmp_path, "a.txt", "retry")
    repo = GitLabRepoConfig(name="application", project="grp/app", local_path=tmp_path)
    api.routes = {"/merge_requests": resp(403), "/repository/branches": resp(200, [{"name": "feature/retry"}])}
    result = search_code(GL, [repo], RETRY, ["retry"])
    assert len(result.problems) == 1 and "403" in result.problems[0]
    assert {c.kind for c in result.candidates} == {"code", "branch"}
    assert not any("merge requests" in s for s in result.searched)


def test_failed_blob_search_is_a_problem_and_not_searched(api):
    api.routes = {"/search": resp(500), "/merge_requests": resp(200, []), "/repository/branches": resp(200, [])}
    result = search_code(GL, [REPO], RETRY, ["retry"])
    assert any("500" in p for p in result.problems)
    assert not any("blob search" in s for s in result.searched)


def test_candidate_cap_per_source(tmp_path, api):
    for i in range(30):
        write(tmp_path, f"f{i}.txt", "retry")
    assert len(search_local(tmp_path, ["retry"], max_candidates=10)[0]) == 10
