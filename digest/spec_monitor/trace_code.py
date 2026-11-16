"""Code candidate search for the page-watch trace pass: local clone, GitLab merge requests, branches, blobs."""
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from digest.config import GitLabConfig, GitLabRepoConfig, RetryConfig
from digest.spec_monitor.http import RequestFailed, request_with_retry
from digest.spec_monitor.models import TraceCandidate, TraceSourceError

MAX_FILES = 20000
MAX_FILE_BYTES = 1024 * 1024
MAX_DETAIL_LINES = 3
_IGNORED_DIRS = {".git", "node_modules", "target", "build", "dist", "bin", "obj", ".venv", "vendor"}
_PER_PAGE = 20
_GIT_TIMEOUT = 10


@dataclass
class CodeSearch:
    candidates: List[TraceCandidate] = field(default_factory=list)
    searched: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _checkout_note(root: Path) -> str:
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    when = _git(root, "log", "-1", "--format=%cI")
    if branch is None or when is None:
        return "no git repository"
    try:
        day = datetime.fromisoformat(when).date().isoformat()
    except ValueError:
        day = when
    return f"branch {branch}, last commit {day}"


def _is_binary(head: bytes) -> bool:
    return b"\x00" in head


def search_local(root: Path, terms: List[str], *, max_candidates: int = 10) -> Tuple[List[TraceCandidate], str]:
    if not root.is_dir():
        raise TraceSourceError(f"local folder {root} not found")
    needles = [t.casefold() for t in terms if t.strip()]
    hits: List[Tuple[int, str, List[str]]] = []
    seen = unreadable = 0
    capped = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORED_DIRS)
        for name in sorted(filenames):
            if seen >= MAX_FILES:
                capped = True
                break
            seen += 1
            path = Path(dirpath) / name
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                unreadable += 1
                continue
            if _is_binary(data[:4096]):
                continue
            text = data.decode("utf-8", errors="replace")
            lowered = text.casefold()
            matched = [n for n in needles if n in lowered]
            if not matched:
                continue
            lines = [ln.strip()[:160] for ln in text.splitlines() if any(n in ln.casefold() for n in matched)]
            hits.append((len(matched), path.relative_to(root).as_posix(), lines[:MAX_DETAIL_LINES]))
        if capped:
            break
    hits.sort(key=lambda h: (-h[0], h[1]))
    candidates = [
        TraceCandidate(kind="code", ref=rel, label=rel, url=str(root / rel), detail="\n".join(lines))
        for _, rel, lines in hits[:max_candidates]
    ]
    notes = [_checkout_note(root)]
    if capped:
        notes.append(f"search stopped after {MAX_FILES} files, later files were not searched")
    if unreadable:
        notes.append(f"{unreadable} file(s) unreadable")
    return candidates, "; ".join(notes)


class GitLabTrace:
    def __init__(self, gitlab: GitLabConfig, retry: RetryConfig):
        self.gitlab = gitlab
        self.retry = retry

    def _api(self, repo: GitLabRepoConfig) -> str:
        project = repo.project if repo.project.isdigit() else quote(repo.project, safe="")
        return f"{self.gitlab.url}/api/v4/projects/{project}"

    def _get(self, repo: GitLabRepoConfig, path: str, params: Dict[str, object], what: str) -> list:
        url = f"{self._api(repo)}{path}"
        try:
            resp = request_with_retry("GET", url, retry=self.retry,
                                      headers={"PRIVATE-TOKEN": self.gitlab.api_token}, params=params)
        except RequestFailed as exc:
            raise TraceSourceError(f"GitLab {repo.name}: {what} failed: {exc.last_error}") from exc
        if not resp.ok:
            raise TraceSourceError(f"GitLab {repo.name}: {what} failed (HTTP {resp.status_code})")
        return resp.json() or []

    def merge_requests(self, repo: GitLabRepoConfig, terms: List[str]) -> List[TraceCandidate]:
        found: Dict[str, TraceCandidate] = {}
        for term in terms:
            rows = self._get(repo, "/merge_requests",
                             {"state": "opened", "search": term, "per_page": _PER_PAGE}, "merge request search")
            for r in rows:
                ref = f"!{r['iid']}"
                found.setdefault(ref, TraceCandidate(
                    kind="mr", ref=ref, label=r.get("title") or ref, url=r.get("web_url") or "",
                    detail=r.get("source_branch") or "", status="open"))
        return list(found.values())

    def branches(self, repo: GitLabRepoConfig, terms: List[str]) -> List[TraceCandidate]:
        found: Dict[str, TraceCandidate] = {}
        for term in terms:
            rows = self._get(repo, "/repository/branches", {"search": term, "per_page": _PER_PAGE}, "branch search")
            for r in rows:
                name = r.get("name")
                if not name or name == repo.default_branch:
                    continue
                url = f"{self.gitlab.url}/{repo.project}/-/tree/{quote(name, safe='/')}" if not repo.project.isdigit() else ""
                found.setdefault(name, TraceCandidate(kind="branch", ref=name, label=name, url=url))
        return list(found.values())

    def blobs(self, repo: GitLabRepoConfig, terms: List[str]) -> List[TraceCandidate]:
        found: Dict[str, TraceCandidate] = {}
        for term in terms:
            rows = self._get(repo, "/search", {"scope": "blobs", "search": term, "ref": repo.default_branch,
                                               "per_page": _PER_PAGE}, "blob search")
            for r in rows:
                path = r.get("path")
                if not path:
                    continue
                url = (f"{self.gitlab.url}/{repo.project}/-/blob/{quote(repo.default_branch, safe='')}/{quote(path)}"
                       if not repo.project.isdigit() else "")
                found.setdefault(path, TraceCandidate(
                    kind="code", ref=path, label=path, url=url, detail=(r.get("data") or "").strip()[:160]))
        return list(found.values())


def search_code(gitlab: GitLabConfig, repos: List[GitLabRepoConfig], retry: RetryConfig,
                terms: List[str]) -> CodeSearch:
    result = CodeSearch()
    trace = GitLabTrace(gitlab, retry)
    for repo in repos:
        use_blobs = repo.local_path is None
        if repo.local_path is not None:
            try:
                found, note = search_local(repo.local_path, terms)
                result.candidates.extend(found)
                result.searched.append(f"{repo.name}: local folder {repo.local_path} ({note})")
            except TraceSourceError as exc:
                result.problems.append(f"{repo.name}: {exc}, GitLab search used instead")
                use_blobs = True
        if use_blobs:
            try:
                result.candidates.extend(trace.blobs(repo, terms))
                result.searched.append(f"{repo.name}: GitLab blob search, default branch {repo.default_branch}")
            except TraceSourceError as exc:
                result.problems.append(str(exc))
        try:
            result.candidates.extend(trace.merge_requests(repo, terms))
            result.searched.append(f"{repo.name}: open merge requests")
        except TraceSourceError as exc:
            result.problems.append(str(exc))
        try:
            result.candidates.extend(trace.branches(repo, terms))
            result.searched.append(f"{repo.name}: branches")
        except TraceSourceError as exc:
            result.problems.append(str(exc))
    return result
