from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from digest.config import GitLabConfig, GitLabDocumentAlias, GitLabRepoConfig, RetryConfig
from digest.spec_monitor import gitlab as gl
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.drawio import DiagramDiff
from digest.spec_monitor.gitlab import GitLabMonitor
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import Classification, SourceKind, SourceRef, SpecInfo

BASE = "https://gitlab.example.com/api/v4/projects/grp%2Frepo"
OLD, NEW, HEAD = "a" * 40, "b" * 40, "c" * 40
PATH = "docs/adr/0028-x.md"


def resp(status=200, json_data=None, content=b""):
    r = MagicMock()
    r.status_code = status
    r.ok = status < 400
    r.json.return_value = json_data
    r.content = content
    r.headers = {}
    return r


class FakeGitLab:
    def __init__(self):
        self.calls = []
        self.probe = resp(200, {"id": 1})
        self.branches = {"main": {"name": "main"}}
        self.commits = {"main": {PATH: [{"id": NEW, "committed_date": "2026-11-05T10:00:00.000+01:00"}]}}
        self.commit_by_sha = {}
        self.until_commits = {}
        self.raw = {(OLD, PATH): b"old text", (NEW, PATH): b"new text"}
        self.compare = {}
        self.boom = None
        self.tree = {}          # folder ('' = root) -> list of {"path", "type"} entries
        self.search_hits = []
        self.search_status = 200
        self.compare_status = 200

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.boom:
            raise self.boom
        params = kw.get("params") or {}
        if url == BASE:
            if isinstance(self.probe, Exception):
                raise self.probe
            return self.probe
        if url.endswith("/repository/tree"):
            entries = self.tree.get(params.get("path", ""), [])
            per, page = params["per_page"], params.get("page", 1)
            return resp(200, entries[(page - 1) * per: page * per])
        if url.endswith("/search"):
            return resp(self.search_status, self.search_hits)
        if "/repository/branches/" in url:
            name = url.rsplit("/", 1)[1]
            return resp(200, self.branches[name]) if name in self.branches else resp(404)
        if url.endswith("/repository/compare"):
            return resp(self.compare_status, {"commits": self.compare.get((params["from"], params["to"]), [])})
        if "/repository/commits/" in url:
            sha = url.rsplit("/", 1)[1]
            return resp(200, self.commit_by_sha[sha]) if sha in self.commit_by_sha else resp(404)
        if url.endswith("/repository/commits"):
            ref = params["ref_name"]
            if "until" in params:
                lst = self.until_commits.get((ref, params["path"]), [])
            else:
                lst = self.commits.get(ref, {}).get(params["path"], [])
            return resp(200, lst)
        if "/repository/files/" in url:
            key = (params["ref"], PATH)
            return resp(200, content=self.raw[key]) if key in self.raw else resp(404)
        raise AssertionError(f"unexpected call {url}")

    def urls(self, suffix):
        return [c for c in self.calls if c[1].endswith(suffix)]


@pytest.fixture
def cfg():
    return GitLabConfig(
        url="https://gitlab.example.com",
        api_token="tok",
        default_project="Docs",
        repositories=[GitLabRepoConfig(name="Docs", project="grp/repo", default_branch="main")],
    )


@pytest.fixture
def fake(mocker):
    f = FakeGitLab()
    mocker.patch.object(gl, "request_with_retry", f)
    return f


@pytest.fixture
def textdiff(mocker):
    m = mocker.patch.object(gl, "textdiff")
    m.diff_text.return_value = "- old text\n+ new text"
    m.is_cosmetic.return_value = False
    return m


@pytest.fixture
def mon(cfg, tmp_path):
    return GitLabMonitor(cfg, RetryConfig(attempts=2, backoff_seconds=0), SpecCache(tmp_path))


def ref(**kw):
    base = dict(name="ADR", kind=SourceKind.REPO, repo_name="Docs", repo_path=PATH,
                repo_branch=None, repo_commit=None, baseline_date=date(2026, 11, 3))
    base.update(kw)
    return SourceRef(**base)


def spec(**kw):
    base = dict(page_id="1", title="Spec", url="https://x/1", status="Refined",
                stand_date=date(2026, 10, 27), created=datetime(2026, 9, 1))
    base.update(kw)
    return SpecInfo(**base)


def test_handles_only_repo(mon):
    assert mon.handles(ref())
    assert not mon.handles(SourceRef(name="x", kind=SourceKind.CONFLUENCE))


def test_changed_old_by_date_new_by_default_branch(mon, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": OLD, "committed_date": "2026-11-02T08:00:00Z"}]
    r = mon.resolve(ref(), spec())
    assert r.classification == Classification.CHANGED
    assert r.old.version == OLD and r.new.version == NEW
    assert r.old_text == "old text" and r.new_text == "new text"
    assert r.diff_text == "- old text\n+ new text"
    assert not r.old.inferred
    until = [c[2]["params"]["until"] for c in fake.urls("/repository/commits") if "until" in c[2]["params"]][0]
    assert until.startswith("2026-11-03T23:59:59")
    refs = [c[2]["params"]["ref"] for c in fake.urls("/raw")]
    assert sorted(refs) == sorted([OLD, NEW])
    textdiff.diff_text.assert_called_once_with("old text", "new text")


def test_auth_header_and_timeout(mon, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": OLD}]
    mon.resolve(ref(), spec())
    assert fake.calls
    for _, _, kw in fake.calls:
        assert kw["headers"]["PRIVATE-TOKEN"] == "tok"
        assert kw["retry"].attempts == 2


def test_equal_sha_unchanged_no_raw_fetch(mon, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": NEW}]
    r = mon.resolve(ref(), spec())
    assert r.classification == Classification.UNCHANGED
    assert not fake.urls("/raw")


def test_cosmetic_only(mon, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": OLD}]
    textdiff.is_cosmetic.return_value = True
    assert mon.resolve(ref(), spec()).classification == Classification.COSMETIC_ONLY


def test_identical_content_different_sha_is_unchanged(mon, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": OLD}]
    textdiff.diff_text.return_value = ""
    textdiff.is_cosmetic.return_value = True
    assert mon.resolve(ref(), spec()).classification == Classification.UNCHANGED


def test_stated_commit_used_when_it_exists(mon, fake, textdiff):
    fake.commit_by_sha["abc1234"] = {"id": OLD, "committed_date": "2026-10-30T10:00:00Z"}
    fake.commits[OLD] = {PATH: [{"id": OLD, "committed_date": "2026-10-30T10:00:00Z"}]}
    r = mon.resolve(ref(repo_commit="abc1234"), spec())
    assert r.classification == Classification.CHANGED
    assert r.old.version == OLD and not r.old.inferred
    assert not any("until" in (c[2].get("params") or {}) for c in fake.calls)


def test_stated_commit_missing_falls_back_to_date_with_warning(mon, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": OLD}]
    r = mon.resolve(ref(repo_commit="deadbee"), spec())
    assert r.old.version == OLD
    assert any("deadbee" in w for w in r.warnings)


def test_pinned_branch_gone_falls_back_to_default_with_warning(mon, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": OLD}]
    r = mon.resolve(ref(repo_branch="feature/x"), spec())
    assert r.old.version == OLD
    assert any("feature/x" in w for w in r.warnings)
    assert fake.urls("/repository/commits")[0][2]["params"]["ref_name"] == "main"


def test_pinned_branch_used_for_old_when_it_exists(mon, fake, textdiff):
    fake.branches["rel"] = {"name": "rel"}
    fake.until_commits[("rel", PATH)] = [{"id": OLD}]
    r = mon.resolve(ref(repo_branch="rel"), spec())
    assert r.old.version == OLD


def test_pinned_branch_ahead_of_default_noted(mon, fake, textdiff):
    fake.branches["rel"] = {"name": "rel"}
    fake.until_commits[("rel", PATH)] = [{"id": OLD}]
    fake.compare[("main", "rel")] = [{"id": "c" * 40}]
    r = mon.resolve(ref(repo_branch="rel"), spec())
    assert any("rel" in w and "ahead" in w for w in r.warnings)


def test_pinned_branch_not_ahead_no_note(mon, fake, textdiff):
    fake.branches["rel"] = {"name": "rel"}
    fake.until_commits[("rel", PATH)] = [{"id": OLD}]
    r = mon.resolve(ref(repo_branch="rel"), spec())
    assert not any("ahead" in w for w in r.warnings)


def test_no_date_uses_stand_date_inferred(mon, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": OLD}]
    r = mon.resolve(ref(baseline_date=None), spec())
    assert r.old.inferred and "2026-10-27" in r.old.inferred_reason
    assert [c[2]["params"]["until"] for c in fake.urls("/repository/commits") if "until" in c[2]["params"]][0].startswith("2026-10-27T23:59:59")


def test_no_date_no_stand_date_uses_created_inferred(mon, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": OLD}]
    r = mon.resolve(ref(baseline_date=None), spec(stand_date=None))
    assert r.old.inferred and "2026-09-01" in r.old.inferred_reason


def test_no_baseline_at_all_is_error(mon, fake, textdiff):
    r = mon.resolve(ref(baseline_date=None), spec(stand_date=None, created=None))
    assert r.classification == Classification.ERROR and r.errors


def test_no_commit_before_baseline_is_error(mon, fake, textdiff):
    r = mon.resolve(ref(), spec())
    assert r.classification == Classification.ERROR
    assert "file not found" in r.errors[0]


def test_file_missing_on_default_branch_is_error(mon, fake, textdiff):
    fake.commits["main"] = {}
    fake.until_commits[("main", PATH)] = [{"id": OLD}]
    r = mon.resolve(ref(), spec())
    assert r.classification == Classification.ERROR
    assert "not found" in r.errors[0]


def test_raw_404_reports_sha(mon, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": OLD}]
    del fake.raw[(NEW, PATH)]
    r = mon.resolve(ref(), spec())
    assert r.classification == Classification.ERROR
    assert f"file not found at {NEW}" in r.errors[0]


def test_drawio_changes_classified(cfg, tmp_path, mocker, textdiff):
    p = "diagrams/arch.drawio"
    f = FakeGitLab()
    f.commits["main"] = {p: [{"id": NEW}]}
    f.until_commits[("main", p)] = [{"id": OLD}]
    orig = f.__call__

    def handler(method, url, **kw):
        if "/repository/files/" in url:
            assert "diagrams%2Farch.drawio" in url
            return resp(200, content=b"<old/>" if kw["params"]["ref"] == OLD else b"<new/>")
        return orig(method, url, **kw)

    mocker.patch.object(gl, "request_with_retry", handler)
    dd = mocker.patch.object(gl, "drawio")
    dd.diff_diagrams.return_value = DiagramDiff(changes=["Node added: X"], warnings=["w1"])
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(repo_path=p), spec())
    assert r.classification == Classification.CHANGED
    assert r.diagram_notes == ["Node added: X"]
    assert "w1" in r.warnings
    textdiff.diff_text.assert_not_called()

    dd.diff_diagrams.return_value = DiagramDiff(layout_only=True)
    m2 = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path / "b"))
    assert m2.resolve(ref(repo_path=p), spec()).classification == Classification.COSMETIC_ONLY

    dd.diff_diagrams.side_effect = ValueError("bad")
    m3 = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path / "c"))
    r3 = m3.resolve(ref(repo_path=p), spec())
    assert r3.classification == Classification.CHANGED
    assert any("not compared" in n for n in r3.diagram_notes)


def test_unreachable_probe_once_per_repo(mon, fake, textdiff):
    fake.probe = RequestFailed(BASE, 2, "ConnectionError: dns")
    r1 = mon.resolve(ref(), spec())
    r2 = mon.resolve(ref(name="Other", repo_path="docs/other.md"), spec())
    for r in (r1, r2):
        assert r.classification == Classification.ERROR
        assert r.errors == ["repository not reachable (ConnectionError: dns)"]
        assert r.attempts == 2
    assert len(fake.calls) == 1


@pytest.mark.parametrize("status", [401, 403, 404])
def test_probe_http_error_is_unreachable(mon, fake, textdiff, status):
    fake.probe = resp(status)
    r = mon.resolve(ref(), spec())
    assert r.classification == Classification.ERROR
    assert r.errors == [f"repository not reachable (HTTP {status})"]
    mon.resolve(ref(), spec())
    assert len(fake.calls) == 1


def test_probe_success_cached_per_repo(mon, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": NEW}]
    mon.resolve(ref(), spec())
    mon.resolve(ref(), spec())
    assert len(fake.urls("%2Frepo")) == 1


def test_repo_not_configured_not_monitored(mon, fake, textdiff):
    r = mon.resolve(ref(repo_name="Nope"), spec())
    assert r.classification == Classification.NOT_MONITORED
    assert "Nope" in r.warnings[0] or "Nope" in "".join(r.warnings)
    assert not fake.calls


def test_default_project_used_when_no_repo_name(mon, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": NEW}]
    assert mon.resolve(ref(repo_name=None), spec()).classification == Classification.UNCHANGED


def test_no_default_project_not_monitored(cfg, tmp_path, fake):
    cfg.default_project = None
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    assert m.resolve(ref(repo_name=None), spec()).classification == Classification.NOT_MONITORED


def test_raw_cached_by_sha(cfg, tmp_path, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": OLD}]
    cache = SpecCache(tmp_path)
    GitLabMonitor(cfg, RetryConfig(), cache).resolve(ref(), spec())
    assert len(fake.urls("/raw")) == 2
    GitLabMonitor(cfg, RetryConfig(), cache).resolve(ref(), spec())
    assert len(fake.urls("/raw")) == 2


def test_request_failure_during_resolve_is_error_with_attempts(mon, fake, textdiff, mocker):
    fake.until_commits[("main", PATH)] = [{"id": OLD}]
    orig = fake.__call__

    def flaky(method, url, **kw):
        if "/repository/files/" in url:
            raise RequestFailed(url, 3, "HTTP 503")
        return orig(method, url, **kw)

    mocker.patch.object(gl, "request_with_retry", flaky)
    r = mon.resolve(ref(), spec())
    assert r.classification == Classification.ERROR
    assert r.attempts == 3 and "HTTP 503" in r.errors[0]


def test_never_raises_on_unexpected_error(mon, fake, textdiff):
    fake.until_commits[("main", PATH)] = [{"id": OLD}]
    textdiff.diff_text.side_effect = RuntimeError("boom")
    r = mon.resolve(ref(), spec())
    assert r.classification == Classification.ERROR and "boom" in r.errors[0]


# ---- document location (docs folders, aliases, name search) ----

def blobs(*paths):
    return [{"path": p, "type": "blob"} for p in paths]


def located(fake, path):
    fake.commits["main"][path] = [{"id": NEW}]
    fake.until_commits[("main", path)] = [{"id": NEW}]


def search_cfg(cfg, folders, documents=None):
    repo = cfg.repositories[0]
    repo.docs_folders = folders
    repo.documents = documents or []
    return cfg


@pytest.fixture
def sfake(fake):
    fake.branches["main"] = {"name": "main", "commit": {"id": HEAD}}
    fake.commits["main"] = {}
    return fake


def tree_calls(fake):
    return fake.urls("/repository/tree")


def test_adr_number_matches_zero_padded_file(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"])
    sfake.tree["docs"] = blobs("docs/adr/0027-other.md", "docs/adr/0028-api-contract-conventions.md", "docs/readme.md")
    located(sfake, "docs/adr/0028-api-contract-conventions.md")
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="ADR-0028 «API contract conventions» im Repository", repo_name=None, repo_path=None), spec())
    assert r.classification == Classification.UNCHANGED
    assert r.ref.repo_path == "docs/adr/0028-api-contract-conventions.md"
    assert "document located at docs/adr/0028-api-contract-conventions.md (folder search)" in r.warnings
    p = tree_calls(sfake)[0][2]["params"]
    assert p["path"] == "docs" and p["recursive"] == "true" and p["per_page"] == 100 and p["ref"] == HEAD


def test_name_found_as_adoc_in_root_folder(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, [""])
    sfake.tree[""] = blobs("README.md", "API-Konzept.adoc", "other.adoc", "img/api_konzept.png")
    located(sfake, "API-Konzept.adoc")
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="API-Konzept, Dokument im Diagramm-Repository (Branch main, Commit abc1234)",
                      repo_name=None, repo_path=None), spec())
    assert r.classification == Classification.UNCHANGED
    assert r.ref.repo_path == "API-Konzept.adoc"
    assert "path" not in tree_calls(sfake)[0][2]["params"]


def test_underscore_lowercase_stem_equals_name(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"])
    sfake.tree["docs"] = blobs("docs/api_konzept.md", "docs/misc.md")
    located(sfake, "docs/api_konzept.md")
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="API-Konzept", repo_path=None), spec())
    assert r.ref.repo_path == "docs/api_konzept.md"


def test_explicit_path_retried_under_docs_folder(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"])
    located(sfake, "docs/adr/0028-x.md")
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(repo_path="adr/0028-x.md"), spec())
    assert r.classification == Classification.UNCHANGED
    assert r.ref.repo_path == "docs/adr/0028-x.md"
    assert "document located at docs/adr/0028-x.md (path)" in r.warnings
    assert not tree_calls(sfake)


def test_explicit_path_at_head_needs_no_search(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"])
    located(sfake, PATH)
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(), spec())
    assert r.ref.repo_path == PATH and not tree_calls(sfake)


def test_explicit_path_basename_searched_below_folders(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"])
    sfake.tree["docs"] = blobs("docs/decisions/0028-x.md", "docs/z.md")
    located(sfake, "docs/decisions/0028-x.md")
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(repo_path="old/place/0028-x.md"), spec())
    assert r.ref.repo_path == "docs/decisions/0028-x.md"
    assert "document located at docs/decisions/0028-x.md (folder search)" in r.warnings


def test_alias_wins_over_name_search(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"], [GitLabDocumentAlias(match="API-Konzept", path="concepts/api.adoc")])
    sfake.tree["docs"] = blobs("docs/API-Konzept.adoc")
    located(sfake, "concepts/api.adoc")
    located(sfake, "docs/API-Konzept.adoc")
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="API-Konzept, Dokument im Repository", repo_path=None), spec())
    assert r.ref.repo_path == "concepts/api.adoc"
    assert "document located at concepts/api.adoc (alias)" in r.warnings
    assert not tree_calls(sfake)


def test_ambiguous_candidates_pick_shortest_and_warn(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"])
    sfake.tree["docs"] = blobs("docs/archive/old/API-Konzept.md", "docs/a/API-Konzept.adoc")
    located(sfake, "docs/a/API-Konzept.adoc")
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="API-Konzept", repo_path=None), spec())
    assert r.ref.repo_path == "docs/a/API-Konzept.adoc"
    assert any("ambiguous: also docs/archive/old/API-Konzept.md" in w for w in r.warnings)


def test_nothing_found_is_error_with_file_count(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"])
    sfake.tree["docs"] = blobs("docs/a.md", "docs/b.md", "docs/c.md")
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="Zzz Unknown, Dokument im Repository", repo_path=None), spec())
    assert r.classification == Classification.ERROR
    assert r.errors == ["document not found under docs of Docs (searched 3 files)"]


def test_tree_listing_cached_by_head_sha(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"])
    sfake.tree["docs"] = blobs("docs/API-Konzept.adoc")
    located(sfake, "docs/API-Konzept.adoc")
    cache = SpecCache(tmp_path)
    m = GitLabMonitor(cfg, RetryConfig(), cache)
    m.resolve(ref(name="API-Konzept", repo_path=None), spec())
    assert len(tree_calls(sfake)) == 1
    m.resolve(ref(name="API-Konzept", repo_path=None), spec())
    GitLabMonitor(cfg, RetryConfig(), cache).resolve(ref(name="API-Konzept", repo_path=None), spec())
    assert len(tree_calls(sfake)) == 1
    sfake.branches["main"] = {"name": "main", "commit": {"id": "d" * 40}}
    GitLabMonitor(cfg, RetryConfig(), cache).resolve(ref(name="API-Konzept", repo_path=None), spec())
    assert len(tree_calls(sfake)) == 2


def test_tree_pagination(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"])
    sfake.tree["docs"] = blobs(*[f"docs/f{i}.md" for i in range(230)], "docs/API-Konzept.adoc")
    located(sfake, "docs/API-Konzept.adoc")
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="API-Konzept", repo_path=None), spec())
    assert r.ref.repo_path == "docs/API-Konzept.adoc"
    assert [c[2]["params"]["page"] for c in tree_calls(sfake)] == [1, 2, 3]


def test_tree_listing_capped_with_warning(cfg, tmp_path, sfake, textdiff, monkeypatch):
    monkeypatch.setattr(gl, "_TREE_CAP", 3000)
    search_cfg(cfg, ["docs"])
    sfake.tree["docs"] = blobs("docs/API-Konzept.adoc", *[f"docs/f{i}.md" for i in range(3200)])
    located(sfake, "docs/API-Konzept.adoc")
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="API-Konzept", repo_path=None), spec())
    assert r.ref.repo_path == "docs/API-Konzept.adoc"
    assert any("3000" in w and "capped" in w for w in r.warnings)


def test_blob_search_fallback(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"])
    sfake.tree["docs"] = blobs("docs/foo.md")
    sfake.search_hits = [{"path": "elsewhere/netz.adoc"}, {"path": "docs/concept/netz.adoc"}]
    located(sfake, "docs/concept/netz.adoc")
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="Netzkonzept, Dokument im Repository", repo_path=None), spec())
    assert r.ref.repo_path == "docs/concept/netz.adoc"
    assert "document located at docs/concept/netz.adoc (blob search)" in r.warnings
    p = sfake.urls("/search")[0][2]["params"]
    assert p["scope"] == "blobs" and p["search"] == "Netzkonzept" and p["ref"] == "main"


def test_unreachable_repo_single_probe_without_path(mon, fake, textdiff):
    fake.probe = RequestFailed(BASE, 2, "ConnectionError: dns")
    r = mon.resolve(ref(repo_path=None, name="API-Konzept"), spec())
    assert r.classification == Classification.ERROR
    assert len(fake.calls) == 1


def test_requests_use_configured_timeout(cfg, tmp_path, mocker, textdiff):
    f = FakeGitLab()
    f.until_commits[("main", PATH)] = [{"id": OLD}]
    mocker.patch("digest.spec_monitor.http.requests.request", side_effect=f)
    mon = GitLabMonitor(cfg, RetryConfig(attempts=1, backoff_seconds=0, timeout_seconds=77), SpecCache(tmp_path))
    mon.resolve(ref(), spec())
    assert f.calls and all(kw["timeout"] == 77 for _, _, kw in f.calls)


# ---- errors and warnings must not be dropped ----

def test_failure_keeps_warnings_collected_before_it(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"], [GitLabDocumentAlias(match="API-Konzept", path="concepts/missing.adoc")])
    sfake.tree["docs"] = blobs("docs/a.md")
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="API-Konzept, Dokument", repo_path=None), spec())
    assert r.classification == Classification.ERROR
    assert "document not found" in r.errors[0]
    assert "alias path concepts/missing.adoc not found" in r.errors[0]


def test_capped_tree_listing_is_in_the_error(cfg, tmp_path, sfake, textdiff, monkeypatch):
    monkeypatch.setattr(gl, "_TREE_CAP", 2)
    search_cfg(cfg, ["docs"])
    sfake.tree["docs"] = blobs("docs/a.md", "docs/b.md", "docs/c.md")
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="Zzz Unknown, Dokument", repo_path=None), spec())
    assert r.classification == Classification.ERROR
    assert "capped at 2 entries" in r.errors[0]


def test_failed_blob_search_is_reported(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"])
    sfake.tree["docs"] = blobs("docs/a.md")
    sfake.search_status = 403
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="Zzz Unknown, Dokument", repo_path=None), spec())
    assert r.classification == Classification.ERROR
    assert "blob search" in r.errors[0] and "HTTP 403" in r.errors[0]


def test_blob_search_pages_through_hits(cfg, tmp_path, sfake, textdiff, mocker):
    search_cfg(cfg, ["docs"])
    sfake.tree["docs"] = blobs("docs/a.md")
    located(sfake, "docs/hit.md")
    pages = []
    orig = sfake.__call__

    def paged(method, url, **kw):
        if url.endswith("/search"):
            page = kw["params"]["page"]
            pages.append(page)
            hits = [{"path": f"other/{i}.md"} for i in range(gl._PER_PAGE)] if page == 1 else [{"path": "docs/hit.md"}]
            return resp(200, hits)
        return orig(method, url, **kw)

    mocker.patch.object(gl, "request_with_retry", paged)
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="Zzz Unknown, Dokument", repo_path=None), spec())
    assert pages == [1, 2]
    assert r.ref.repo_path == "docs/hit.md"


def test_failed_branch_compare_is_warned(mon, fake, textdiff):
    fake.branches["rel"] = {"name": "rel"}
    fake.until_commits[("rel", PATH)] = [{"id": OLD}]
    fake.compare_status = 400
    r = mon.resolve(ref(repo_branch="rel"), spec())
    assert any("could not compare branch rel" in w for w in r.warnings)


def test_ambiguity_warning_survives_unchanged_result(cfg, tmp_path, sfake, textdiff):
    search_cfg(cfg, ["docs"])
    sfake.tree["docs"] = blobs("docs/archive/old/API-Konzept.md", "docs/a/API-Konzept.adoc")
    located(sfake, "docs/a/API-Konzept.adoc")
    sfake.until_commits[("main", "docs/a/API-Konzept.adoc")] = [{"id": NEW}]   # old == new
    m = GitLabMonitor(cfg, RetryConfig(), SpecCache(tmp_path))
    r = m.resolve(ref(name="API-Konzept", repo_path=None), spec())
    assert r.classification == Classification.UNCHANGED
    assert any("ambiguous" in w for w in r.warnings)
