"""GitLab repository monitor: compares a repo file at the stated baseline with the
latest commit touching it on the default branch (GitLab REST v4)."""
import logging
import re
import threading
from datetime import date, datetime
from typing import List, Optional, Tuple
from urllib.parse import quote

from digest.config import GitLabConfig, GitLabRepoConfig, RetryConfig
from digest.spec_monitor import drawio, textdiff
from digest.spec_monitor.cache import SpecCache, hash_key
from digest.spec_monitor.http import RequestFailed, request_with_retry
from digest.spec_monitor.models import (
    Classification,
    SourceKind,
    SourceRef,
    SourceResult,
    SpecInfo,
    VersionPoint,
)

log = logging.getLogger(__name__)

_PER_PAGE = 100
_TREE_CAP = 200000   # safety ceiling only; the listing is paged to the end
_BLOB_PAGES = 5
_DOC_EXTS = {".md", ".adoc", ".rst", ".txt", ".puml", ".drawio", ".yaml", ".yml", ".json"}


class _Fail(Exception):
    def __init__(self, message: str, attempts: int = 1):
        super().__init__(message)
        self.message = message
        self.attempts = attempts


def _parse_when(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class GitLabMonitor:
    def __init__(self, cfg: GitLabConfig, retry: RetryConfig, cache: SpecCache):
        self.cfg = cfg
        self.retry = retry
        self.cache = cache
        self._probes = {}   # project -> None (reachable) | (reason, attempts)
        self._lock = threading.Lock()

    def handles(self, ref: SourceRef) -> bool:
        return ref.kind == SourceKind.REPO

    def resolve(self, ref: SourceRef, spec: SpecInfo) -> SourceResult:
        try:
            return self._resolve(ref, spec)
        except _Fail as exc:
            return self._error(ref, exc.message, exc.attempts)
        except RequestFailed as exc:
            return self._error(ref, exc.last_error, exc.attempts)
        except Exception as exc:  # noqa: BLE001 -- monitors never raise
            log.exception("gitlab monitor failed for %s", ref.name)
            return self._error(ref, f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _error(ref: SourceRef, message: str, attempts: int = 1) -> SourceResult:
        return SourceResult(ref=ref, classification=Classification.ERROR, errors=[message], attempts=attempts)

    def _find_repo(self, ref: SourceRef) -> Optional[GitLabRepoConfig]:
        wanted = ref.repo_name or self.cfg.default_project
        if not wanted:
            return None
        for repo in self.cfg.repositories:
            if wanted.lower() in (repo.name.lower(), repo.project.lower()):
                return repo
        return None

    def _api(self, repo: GitLabRepoConfig) -> str:
        project = repo.project if repo.project.isdigit() else quote(repo.project, safe="")
        return f"{self.cfg.url}/api/v4/projects/{project}"

    def _get(self, url: str, params: Optional[dict] = None):
        return request_with_retry(
            "GET", url, retry=self.retry,
            headers={"PRIVATE-TOKEN": self.cfg.api_token}, params=params,
        )

    def _probe(self, repo: GitLabRepoConfig) -> Optional[Tuple[str, int]]:
        with self._lock:
            if repo.project in self._probes:
                return self._probes[repo.project]
            try:
                resp = self._get(self._api(repo))
                outcome = None if resp.ok else (f"HTTP {resp.status_code}", 1)
            except RequestFailed as exc:
                outcome = (exc.last_error, exc.attempts)
            except Exception as exc:  # noqa: BLE001
                outcome = (f"{type(exc).__name__}: {exc}", 1)
            self._probes[repo.project] = outcome
            return outcome

    def _resolve(self, ref: SourceRef, spec: SpecInfo) -> SourceResult:
        repo = self._find_repo(ref)
        if repo is None:
            name = ref.repo_name or self.cfg.default_project or "(none)"
            return SourceResult(
                ref=ref, classification=Classification.NOT_MONITORED,
                warnings=[f"repository '{name}' is not configured in gitlab.repositories"],
            )
        failure = self._probe(repo)
        if failure:
            return self._error(ref, f"repository not reachable ({failure[0]})", failure[1])

        api = self._api(repo)
        warnings: List[str] = []
        try:
            return self._compare(api, repo, ref, spec, warnings)
        except _Fail as exc:
            # notes gathered before the failure (alias not found, listing capped, ...) explain it
            if warnings:
                raise _Fail(f"{exc.message} [{'; '.join(warnings)}]", exc.attempts) from exc
            raise

    def _compare(self, api, repo: GitLabRepoConfig, ref: SourceRef, spec: SpecInfo, warnings: List[str]) -> SourceResult:
        default = repo.default_branch

        path, new_commit = self._locate(api, repo, ref, warnings)
        if new_commit is None:
            new_commit = self._last_commit(api, path, default)
        if new_commit is None:
            raise _Fail(f"file not found on branch {default}")
        new = self._point(repo, new_commit)

        old = self._resolve_old(api, repo, ref, spec, path, warnings)

        self._note_branch_ahead(api, ref, default, warnings)

        result = SourceResult(ref=ref, classification=Classification.UNCHANGED, old=old, new=new, warnings=warnings)
        if old.version == new.version:
            return result

        old_raw = self._raw(api, repo, path, old.version)
        new_raw = self._raw(api, repo, path, new.version)
        if old_raw == new_raw:
            return result

        if path.lower().endswith(".drawio"):
            return self._drawio(result, old_raw, new_raw)

        old_text = old_raw.decode("utf-8", errors="replace")
        new_text = new_raw.decode("utf-8", errors="replace")
        diff = textdiff.diff_text(old_text, new_text)
        result.old_text, result.new_text, result.diff_text = old_text, new_text, diff
        if not diff:
            result.classification = Classification.UNCHANGED
        elif textdiff.is_cosmetic(diff):
            result.classification = Classification.COSMETIC_ONLY
        else:
            result.classification = Classification.CHANGED
        return result

    # ---- document location ----

    @staticmethod
    def _folders(repo: GitLabRepoConfig) -> List[str]:
        return [f.strip().strip("/") for f in (repo.docs_folders or [""])]

    def _locate(self, api, repo, ref, warnings) -> Tuple[str, Optional[dict]]:
        """Find the repo path of the referenced document (alias > stated path > name search).
        Sets ref.repo_path and records how it was found; raises _Fail when nothing is found."""
        default = repo.default_branch
        folders = self._folders(repo)
        text = " ".join(filter(None, [ref.name, ref.title, ref.used_for, ref.version_text, ref.note, ref.repo_path])).casefold()
        exists_cache = {}

        def exists(p: str) -> Optional[dict]:
            if p not in exists_cache:
                exists_cache[p] = self._last_commit(api, p, default)
            return exists_cache[p]

        def found(p: str, how: str, commit: Optional[dict] = None) -> Tuple[str, Optional[dict]]:
            ref.repo_path = p
            warnings.insert(0, f"document located at {p} ({how})")
            return p, commit or exists_cache.get(p)

        for alias in repo.documents:
            if alias.match and alias.match.casefold() in text:
                p = alias.path.strip().lstrip("/")
                commit = exists(p)
                if commit:
                    return found(p, "alias", commit)
                warnings.append(f"alias path {p} not found on branch {default}")

        stated = (ref.repo_path or "").strip().lstrip("/")
        if stated:
            for p in [stated] + [f"{f}/{stated}" for f in folders if f]:
                commit = exists(p)
                if commit:
                    return found(p, "path", commit)

        files = self._tree_files(api, repo, folders, warnings)

        if stated:
            base = stated.rsplit("/", 1)[-1].casefold()
            hits = sorted((p for p in files if p.rsplit("/", 1)[-1].casefold() == base), key=lambda p: (len(p), p))
            if hits:
                self._note_ambiguity(hits[1:], warnings)
                return found(hits[0], "folder search")

        scored = self._score(ref, files)
        if scored:
            top = scored[0][0]
            self._note_ambiguity([p for s, p in scored[1:] if s == top], warnings)
            return found(scored[0][1], "folder search")

        phrase = self._phrase(ref.name)
        if phrase:
            p = self._blob_search(api, repo, phrase, folders, warnings)
            if p:
                return found(p, "blob search")

        label = ", ".join(f or "/" for f in folders)
        raise _Fail(f"document not found under {label} of {repo.name} (searched {len(files)} files)")

    @staticmethod
    def _note_ambiguity(others: List[str], warnings: List[str]) -> None:
        if others:
            warnings.append("ambiguous: also " + ", ".join(others[:3]))

    def _tree_files(self, api, repo, folders, warnings) -> List[str]:
        head = self._head_sha(api, repo.default_branch)
        files: List[str] = []
        for folder in folders:
            key = hash_key(repo.project, folder, head or "")
            cached = self.cache.get_json("gitlab-tree", key) if head else None
            if cached is None:
                cached = self._list_tree(api, folder, head or repo.default_branch)
                if head:
                    self.cache.put_json("gitlab-tree", key, cached)
            if cached["capped"]:
                warnings.append(f"file listing of '{folder or '/'}' capped at {_TREE_CAP} entries, document may be missed")
            files.extend(cached["paths"])
        return list(dict.fromkeys(files))

    def _head_sha(self, api: str, branch: str) -> Optional[str]:
        resp = self._get(f"{api}/repository/branches/{quote(branch, safe='')}")
        if not resp.ok:
            return None
        return ((resp.json() or {}).get("commit") or {}).get("id")

    def _list_tree(self, api: str, folder: str, ref_name: str) -> dict:
        paths: List[str] = []
        seen = 0
        capped = False
        page = 1
        while True:
            params = {"recursive": "true", "per_page": _PER_PAGE, "ref": ref_name, "page": page}
            if folder:
                params["path"] = folder
            resp = self._get(f"{api}/repository/tree", params)
            if not resp.ok:
                raise _Fail(f"file listing of '{folder or '/'}' failed (HTTP {resp.status_code})")
            entries = resp.json() or []
            for e in entries:
                if seen >= _TREE_CAP:
                    capped = True
                    break
                seen += 1
                if e.get("type") == "blob" and e.get("path"):
                    paths.append(e["path"])
            if capped or len(entries) < _PER_PAGE:
                break
            if seen >= _TREE_CAP:
                capped = True  # more pages may follow
                break
            page += 1
        return {"paths": paths, "capped": capped}

    @staticmethod
    def _phrase(name: str) -> str:
        head = re.split(r"[,(]|\s(?:im|in)\s", name or "", maxsplit=1, flags=re.IGNORECASE)[0]
        return head.translate({ord(c): None for c in "«»\"'„“”‚‘’"}).strip()[:100]

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[\W_]+", "", s.casefold())

    def _score(self, ref: SourceRef, files: List[str]) -> List[Tuple[int, str]]:
        phrase = self._phrase(ref.name)
        phrase_n = self._norm(phrase)
        phrase_tokens = {t for t in re.split(r"[\W_]+", phrase.casefold()) if len(t) >= 3}
        adr = re.search(r"ADR[- ]?0*(\d+)", " ".join(filter(None, [ref.name, ref.title])), re.IGNORECASE)
        adr_n = adr.group(1) if adr else None
        scored = []
        for path in files:
            base = path.rsplit("/", 1)[-1]
            stem, dot, ext = base.rpartition(".")
            if not dot:
                stem, ext = base, ""
            stem_n = self._norm(stem)
            if not stem_n:
                continue
            score = 0
            if adr_n and re.match(rf"(?:adr[-_ ]?)?0*{adr_n}(?!\d)", stem, re.IGNORECASE):
                score = max(score, 100)
            if phrase_n:
                if stem_n == phrase_n:
                    score = max(score, 80)
                elif len(stem_n) >= 4 and stem_n in phrase_n:
                    score = max(score, 50)
                elif len(phrase_n) >= 4 and phrase_n in stem_n:
                    score = max(score, 40)
            stem_tokens = {t for t in re.split(r"[\W_]+", stem.casefold()) if t}
            overlap = phrase_tokens & stem_tokens
            if overlap and (len(overlap) >= 2 or max(map(len, overlap)) >= 5):
                score = max(score, int(20 * len(overlap) / len(stem_tokens)))
            if score <= 0:
                continue
            if "." + ext.lower() in _DOC_EXTS:
                score += 5
            scored.append((score, path))
        scored.sort(key=lambda sp: (-sp[0], len(sp[1]), sp[1]))
        return scored

    def _blob_search(self, api, repo, phrase, folders, warnings: List[str]) -> Optional[str]:
        paths: List[str] = []
        for page in range(1, _BLOB_PAGES + 1):
            resp = self._get(
                f"{api}/search",
                {"scope": "blobs", "search": phrase, "ref": repo.default_branch, "per_page": _PER_PAGE, "page": page},
            )
            if not resp.ok:
                warnings.append(f"blob search for '{phrase}' failed (HTTP {resp.status_code}); the document may exist but was not found")
                break
            hits = resp.json() or []
            paths.extend(h.get("path") for h in hits if h.get("path"))
            if len(hits) < _PER_PAGE:
                break
        else:
            warnings.append(f"blob search for '{phrase}' read only the first {_BLOB_PAGES * _PER_PAGE} hits; the document may be missed")
        inside = [p for p in paths if any(not f or p.startswith(f + "/") for f in folders)]
        if not inside:
            return None
        return min(enumerate(inside), key=lambda ip: ("." + ip[1].rsplit(".", 1)[-1].lower() not in _DOC_EXTS, ip[0]))[1]

    def _drawio(self, result: SourceResult, old_raw: bytes, new_raw: bytes) -> SourceResult:
        try:
            dd = drawio.diff_diagrams(
                old_raw.decode("utf-8", errors="replace"), new_raw.decode("utf-8", errors="replace"))
        except ValueError as exc:
            result.classification = Classification.CHANGED
            result.diagram_notes.append(f"diagram changed, not compared ({exc})")
            return result
        result.warnings.extend(dd.warnings)
        result.diagram_notes.extend(dd.changes)
        if dd.layout_only or not dd.changes:
            result.classification = Classification.COSMETIC_ONLY
        else:
            result.classification = Classification.CHANGED
        return result

    def _resolve_old(self, api, repo, ref, spec, path, warnings) -> VersionPoint:
        if ref.repo_commit:
            resp = self._get(f"{api}/repository/commits/{quote(ref.repo_commit, safe='')}")
            if resp.ok:
                sha = (resp.json() or {}).get("id") or ref.repo_commit
                touching = self._last_commit(api, path, sha)
                return self._point(repo, touching or {"id": sha})
            warnings.append(f"stated commit {ref.repo_commit} not found, falling back to date")

        branch = repo.default_branch
        if ref.repo_branch and ref.repo_branch != repo.default_branch:
            if self._branch_exists(api, ref.repo_branch):
                branch = ref.repo_branch
            else:
                warnings.append(f"branch {ref.repo_branch} no longer exists, using {repo.default_branch}")

        inferred_reason = None
        day: Optional[date] = ref.baseline_date
        if day is None and spec.stand_date:
            day, inferred_reason = spec.stand_date, f"from Stand date {spec.stand_date.isoformat()}"
        if day is None and spec.created:
            day = spec.created.date()
            inferred_reason = f"from spec creation date {day.isoformat()}"
        if day is None:
            raise _Fail("no baseline commit, date or Stand date to compare against")

        until = f"{day.isoformat()}T23:59:59Z"
        commit = self._last_commit(api, path, branch, until=until)
        if commit is None:
            raise _Fail(f"file not found on {branch} on or before {day.isoformat()}")
        point = self._point(repo, commit)
        if inferred_reason:
            point.inferred, point.inferred_reason = True, inferred_reason
        return point

    def _note_branch_ahead(self, api, ref, default, warnings) -> None:
        branch = ref.repo_branch
        if not branch or branch == default or not self._branch_exists(api, branch):
            return
        resp = self._get(f"{api}/repository/compare", {"from": default, "to": branch})
        if not resp.ok:
            warnings.append(f"could not compare branch {branch} with {default} (HTTP {resp.status_code}); unmerged work not checked")
        elif (resp.json() or {}).get("commits"):
            warnings.append(f"branch {branch} is ahead of {default} (may be unmerged work)")

    def _branch_exists(self, api: str, branch: str) -> bool:
        resp = self._get(f"{api}/repository/branches/{quote(branch, safe='')}")
        if resp.status_code == 404:
            return False
        if not resp.ok:   # 401/403/...: not "branch is gone"
            raise _Fail(f"branch lookup for {branch} failed (HTTP {resp.status_code})")
        return True

    def _last_commit(self, api: str, path: str, ref_name: str, until: Optional[str] = None) -> Optional[dict]:
        params = {"path": path, "ref_name": ref_name, "per_page": 1}
        if until:
            params["until"] = until
        resp = self._get(f"{api}/repository/commits", params)
        if not resp.ok:
            raise _Fail(f"commit lookup failed (HTTP {resp.status_code})")
        commits = resp.json() or []
        return commits[0] if commits else None

    def _point(self, repo: GitLabRepoConfig, commit: dict) -> VersionPoint:
        sha = commit["id"]
        url = None
        if not repo.project.isdigit():
            url = f"{self.cfg.url}/{repo.project}/-/commit/{sha}"
        return VersionPoint(
            version=sha, when=_parse_when(commit.get("committed_date")), url=url)

    def _raw(self, api: str, repo: GitLabRepoConfig, path: str, sha: str) -> bytes:
        key = hash_key(repo.project, path, sha)
        cached = self.cache.get_text("gitlab_raw", key)
        if cached is not None:
            return cached.encode("utf-8")
        resp = self._get(f"{api}/repository/files/{quote(path, safe='')}/raw", {"ref": sha})
        if resp.status_code == 404:
            raise _Fail(f"file not found at {sha}")
        if not resp.ok:
            raise _Fail(f"HTTP {resp.status_code} fetching file at {sha}")
        content = resp.content
        self.cache.put_text("gitlab_raw", key, content.decode("utf-8", errors="replace"))
        return content
