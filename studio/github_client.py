"""GitHub access behind one ``request()`` transport interface.

Production code (``studio.controller`` and the ``scripts/studio_*``
entry points) only ever talks to ``request(method, path, body)`` plus
the bounded-download helpers. ``GitHubClient`` implements that over
urllib for real runs; ``FakeTransport`` implements the SAME interface
with exact GitHub API-shaped JSON in memory, so tests exercise the real
controller paths offline. No shell interpolation anywhere.
"""
from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request

from . import config, fingerprint, validate


class GitHubError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status
        self.message = message


def _is_not_found(exc: Exception) -> bool:
    return isinstance(exc, GitHubError) and exc.status == 404


class GitHubClient:
    def __init__(self, repo: str, token: str, api: str = "https://api.github.com"):
        self.repo, self.token, self.api = repo, token, api.rstrip("/")

    # -- core transport ---------------------------------------------------
    def request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.api}/repos/{self.repo}{path}", data=data, method=method,
            headers={"Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:  # noqa: BLE001
                pass
            raise GitHubError(e.code, detail) from e

    def request_url(self, method: str, url: str, body: dict | None = None):
        """Absolute-URL request (release upload URLs live off /repos)."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise GitHubError(e.code, "") from e

    def get_or_none(self, path: str):
        try:
            return self.request("GET", path)
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

    def paginated(self, path: str, key: str | None = None) -> list:
        """Fetch all pages (100/page) of a list endpoint."""
        out: list = []
        page = 1
        sep = "&" if "?" in path else "?"
        while True:
            chunk = self.request("GET", f"{path}{sep}per_page=100&page={page}")
            items = chunk.get(key, []) if isinstance(chunk, dict) and key else chunk
            if not isinstance(items, list):
                items = []
            out.extend(items)
            if len(items) < 100:
                return out
            page += 1
            if page > 50:
                raise GitHubError(0, "pagination bound exceeded")

    # -- bounded downloads (Authorization dropped on redirects) -----------
    def _download_bounded(self, url: str, *, auth: bool,
                          max_bytes: int) -> bytes:
        current, use_auth = url, auth
        for _hop in range(6):
            req = urllib.request.Request(current, headers={
                "User-Agent": "studio-controller",
                **({"Authorization": f"Bearer {self.token}"} if use_auth else {})})
            opener = urllib.request.build_opener(_NoRedirect())
            try:
                resp = opener.open(req, timeout=120)  # noqa: S310
            except _Redirect as r:
                check = validate.validate_artifact_storage_url(r.location)
                if not check["ok"]:
                    raise GitHubError(0, f"redirect rejected: {check['failures']}")
                current, use_auth = r.location, False  # drop Authorization
                continue
            except urllib.error.HTTPError as e:
                raise GitHubError(e.code, "download failed") from e
            chunks, total = [], 0
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise GitHubError(0, f"download exceeds {max_bytes} bytes")
                chunks.append(chunk)
            return b"".join(chunks)
        raise GitHubError(0, "too many redirects")

    def download_artifact_bytes(self, artifact_id: int) -> bytes:
        return self._download_bounded(
            f"{self.api}/repos/{self.repo}/actions/artifacts/{artifact_id}/zip",
            auth=True, max_bytes=config.MAX_ARTIFACT_TOTAL_BYTES + 1024 * 1024)

    def download_asset_bytes(self, asset_id: int) -> bytes:
        req = urllib.request.Request(
            f"{self.api}/repos/{self.repo}/releases/assets/{asset_id}",
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/octet-stream"})
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            resp = opener.open(req, timeout=120)  # noqa: S310
        except _Redirect as r:
            return self._download_bounded(r.location, auth=False,
                                          max_bytes=config.MAX_ARTIFACT_FILE_BYTES)
        except urllib.error.HTTPError as e:
            raise GitHubError(e.code, "asset download failed") from e
        chunks, total = [], 0
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > config.MAX_ARTIFACT_FILE_BYTES:
                raise GitHubError(0, "asset download oversize")
            chunks.append(chunk)
        return b"".join(chunks)

    def upload_release_asset(self, release: dict, name: str, data: bytes) -> dict:
        upload_url = str(release["upload_url"]).split("{")[0] + f"?name={name}"
        req = urllib.request.Request(
            upload_url, data=data, method="POST",
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise GitHubError(e.code, "asset upload failed") from e

    # -- convenience wrappers (thin, shaped like the API) ------------------
    def get_issue(self, n: int):
        return self.request("GET", f"/issues/{n}")

    def get_sub_issue_numbers(self, parent: int) -> list[int]:
        subs = self.paginated(f"/issues/{parent}/sub_issues", key=None)
        return [int(s["number"]) for s in subs]

    def find_child(self, parent: int, candidate_id: str, fp: str):
        from . import fingerprint as _fp
        subs = set(self.get_sub_issue_numbers(parent))
        for n in sorted(subs):
            iss = self.get_issue(n)
            if (iss.get("user") or {}).get("login") not in config.BOT_LOGINS:
                continue
            for m in _fp.parse_child_markers(iss.get("body") or ""):
                if (m["parent"], m["candidate_id"], m["fingerprint"]) == (parent, candidate_id, fp):
                    return iss
        # Recover a created-but-not-linked child after an interruption
        # between create and link: scan repo issues (paginated). Only
        # bot-authored issues count — a forged human marker never does.
        try:
            all_issues = self.paginated("/issues?state=all", key=None)
        except GitHubError:
            return None
        for iss in sorted(all_issues, key=lambda i: int(i.get("number", 0))):
            if int(iss.get("number", 0)) in subs:
                continue
            if (iss.get("user") or {}).get("login") not in config.BOT_LOGINS:
                continue
            for m in _fp.parse_child_markers(iss.get("body") or ""):
                if (m["parent"], m["candidate_id"], m["fingerprint"]) == (parent, candidate_id, fp):
                    return iss
        return None

    def create_issue(self, title: str, body: str):
        return self.request("POST", "/issues", {"title": title, "body": body})

    def link_sub_issue(self, parent: int, child_db_id: int):
        # GitHub sub-issues API takes the database id (sub_issue_id).
        return self.request("POST", f"/issues/{parent}/sub_issues",
                            {"sub_issue_id": child_db_id})

    def create_pr(self, title: str, head: str, body: str):
        return self.request("POST", "/pulls",
                            {"title": title, "head": head,
                             "base": config.DEFAULT_BRANCH, "body": body})

    def review_comment(self, pr: int, body: str, commit_id: str | None = None):
        payload: dict = {"body": body, "event": "COMMENT"}
        if commit_id:
            payload["commit_id"] = commit_id
        return self.request("POST", f"/pulls/{pr}/reviews", payload)

    def update_review(self, pr: int, review_id: int, body: str):
        return self.request("PUT", f"/pulls/{pr}/reviews/{review_id}",
                            {"body": body})

    def assign(self, issue: int, login: str):
        return self.request("POST", f"/issues/{issue}/assignees",
                            {"assignees": [login]})


class _Redirect(Exception):
    def __init__(self, location: str):
        self.location = location


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        raise _Redirect(newurl)


# ---------------------------------------------------------------------------
# In-memory transport with exact GitHub API-shaped JSON (tests only).
# ---------------------------------------------------------------------------
class FakeTransport:
    """Implements GitHubClient's interface; records every call."""

    def __init__(self, repo: str = "YusukeIwaki/piano-studio"):
        self.repo = repo
        self.owner, self.name = repo.split("/", 1)
        self.calls: list[tuple[str, str]] = []
        self.issues: dict[int, dict] = {}
        self.comments: dict[int, list[dict]] = {}
        self.pulls: dict[int, dict] = {}
        self.reviews: dict[int, list[dict]] = {}
        self.releases: dict[int, dict] = {}
        self.branches: dict[str, str] = {}
        self.commits: dict[str, dict] = {}
        self.trees: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.runs: dict[int, dict] = {}
        self.artifacts: dict[int, dict] = {}
        self.artifact_bytes: dict[int, bytes] = {}
        self.workflow_id = 4242
        self.tag_refs: dict[str, dict] = {}
        self.tag_objects: dict[str, dict] = {}
        self._next_issue, self._next_pr = 100, 200
        self._next_id, self._seq = 9000, 0
        self._next_release, self._next_asset = 500, 600
        self._next_comment = 700

    # -- seeding helpers ---------------------------------------------------
    def _nid(self) -> int:
        self._next_id += 1
        return self._next_id

    def _sha(self, payload: bytes) -> str:
        self._seq += 1
        return hashlib.sha1(payload + str(self._seq).encode()).hexdigest()

    def seed_issue(self, *, title="source", body="", author=config.OWNER,
                   state="open") -> dict:
        n = self._next_issue
        self._next_issue += 1
        iss = {"number": n, "id": self._nid(), "title": title, "body": body,
               "state": state, "state_reason": None,
               "user": {"login": author}, "closed_by": None, "assignees": [],
               "sub_issues": []}
        self.issues[n] = iss
        return iss

    def seed_tag(self, tag: str, target_sha: str, *, annotated: bool = False) -> str:
        """Seed a git tag ref. Annotated tags point at a tag object
        which in turn points at the commit; lightweight tags point
        directly at the commit. Returns the ref object sha."""
        if annotated:
            tsha = self._sha(b"tag" + target_sha.encode())
            self.tag_objects[tsha] = {"sha": tsha,
                                      "object": {"sha": target_sha, "type": "commit"}}
            self.tag_refs[tag] = {"sha": tsha, "type": "tag"}
            return tsha
        self.tag_refs[tag] = {"sha": target_sha, "type": "commit"}
        return target_sha

    def _ensure_tag_for_release(self, rel: dict) -> None:
        tag = rel.get("tag_name", "")
        if tag and tag not in self.tag_refs and rel.get("target_commitish"):
            self.tag_refs[tag] = {"sha": rel["target_commitish"], "type": "commit"}

    def _branch_commits(self, branch: str) -> list[dict]:
        head = self.branches.get(branch, branch)
        out, seen, cur = [], set(), head
        while cur and cur in self.commits and cur not in seen:
            seen.add(cur)
            c = self.commits[cur]
            login = c.get("author_login", "")
            out.append({"sha": c["sha"], "author": {"login": login},
                        "commit": {"author": {"name": login}}})
            parents = c.get("parents") or []
            cur = parents[0] if parents else None
        return out

    def seed_commit(self, files: dict[str, bytes], *, branch="main") -> str:
        tree_files = {}
        for path, data in files.items():
            bsha = hashlib.sha1(b"blob" + data).hexdigest()
            self.blobs[bsha] = data
            tree_files[path] = bsha
        tsha = self._sha(json.dumps(tree_files, sort_keys=True).encode())
        self.trees[tsha] = {"sha": tsha, "files": dict(tree_files)}
        csha = self._sha(b"commit" + tsha.encode())
        self.commits[csha] = {"sha": csha, "tree_sha": tsha, "parents": [],
                              "author_login": "github-actions[bot]"}
        self.branches[branch] = csha
        return csha

    # -- transport ----------------------------------------------------------
    def request(self, method: str, path: str, body: dict | None = None):
        import copy
        path_only, _, query = path.partition("?")
        self.calls.append((method, path_only))
        body = body or {}
        seg = [s for s in path_only.split("/") if s]
        try:
            result = self._dispatch(method, seg, body, query)
        except GitHubError:
            raise
        result = copy.deepcopy(result)
        # Honour GitHub pagination on list results so paginated()
        # exercises real multi-page behaviour.
        if isinstance(result, list) and "per_page=" in query:
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            try:
                per_page = int(params.get("per_page", "30"))
                page = int(params.get("page", "1"))
            except ValueError:
                per_page, page = 30, 1
            start = (page - 1) * per_page
            result = result[start:start + per_page]
        return result

    def _dispatch(self, method, seg, body, query):  # noqa: C901 - router
        # /issues...
        if seg[:1] == ["issues"]:
            if len(seg) == 1 and method == "GET":
                return [self.issues[k] for k in sorted(self.issues)]
            if len(seg) == 1 and method == "POST":
                return self.seed_issue(title=body.get("title", ""),
                                       body=body.get("body", ""),
                                       author="github-actions[bot]")
            n = int(seg[1])
            iss = self.issues.get(n)
            if iss is None:
                raise GitHubError(404, "issue not found")
            if len(seg) == 2 and method == "GET":
                return iss
            if len(seg) == 2 and method == "PATCH":
                # Standard issue update: state and/or title/body.
                if "state" in body:
                    iss["state"] = body["state"]
                    iss["state_reason"] = body.get("state_reason")
                    if body["state"] == "closed":
                        # GitHub records the authenticated closer.
                        iss["closed_by"] = {"login": body.get("_closed_by", "github-actions[bot]")}
                for _k in ("title", "body"):
                    if _k in body:
                        iss[_k] = body[_k]
                return iss
            if seg[2:] == ["sub_issues"] and method == "GET":
                return [self.issues[x] for x in iss["sub_issues"]]
            if seg[2:] == ["sub_issues"] and method == "POST":
                child = next((i for i in self.issues.values()
                              if i["id"] == body.get("sub_issue_id")), None)
                if child is None:
                    raise GitHubError(404, "sub-issue not found")
                if child["number"] not in iss["sub_issues"]:
                    iss["sub_issues"].append(child["number"])
                return child
            if seg[2:] == ["comments"] and method == "GET":
                return self.comments.get(n, [])
            if seg[2:] == ["comments"] and method == "POST":
                self._next_comment += 1
                c = {"id": self._next_comment, "body": body.get("body", ""),
                     "user": {"login": "github-actions[bot]"}}
                self.comments.setdefault(n, []).append(c)
                return c
            if seg[2:] == ["assignees"] and method == "POST":
                for login in body.get("assignees", []):
                    if {"login": login} not in iss["assignees"]:
                        iss["assignees"].append({"login": login})
                pr = self.pulls.get(n)
                if pr is not None:
                    pr["assignees"] = list(iss["assignees"])
                return iss
        if seg[:1] == ["pulls"]:
            if len(seg) == 1 and method == "POST":
                num = self._next_pr
                self._next_pr += 1
                head_ref = body["head"]
                pr = {"number": num, "id": self._nid(), "title": body.get("title", ""),
                      "body": body.get("body", ""), "state": "open", "merged": False,
                      "merged_at": None, "merged_by": None, "merge_commit_sha": None,
                      "head": {"ref": head_ref, "sha": self.branches.get(head_ref, ""),
                               "repo": {"full_name": self.repo}},
                      "base": {"ref": body.get("base", "main"),
                               "repo": {"full_name": self.repo}},
                      "user": {"login": "github-actions[bot]"}, "assignees": []}
                self.pulls[num] = pr
                # A PR is also an issue in the GitHub API (assignees live there).
                self.issues[num] = {"number": num, "id": pr["id"],
                                    "title": pr["title"], "body": pr["body"],
                                    "state": "open", "state_reason": None,
                                    "user": {"login": "github-actions[bot]"},
                                    "closed_by": None,
                                    "assignees": pr["assignees"], "sub_issues": []}
                return pr
            if len(seg) == 1 and method == "GET":
                out = list(self.pulls.values())
                if "head=" in query:
                    want = query.split("head=", 1)[1].split("&")[0]
                    ref = want.split(":", 1)[-1]
                    out = [p for p in out if p["head"]["ref"] == ref]
                if "state=open" in query:
                    out = [p for p in out if p["state"] == "open"]
                return out
            n = int(seg[1])
            pr = self.pulls.get(n)
            if pr is None:
                raise GitHubError(404, "pull not found")
            if len(seg) == 2 and method == "GET":
                pr["head"]["sha"] = self.branches.get(pr["head"]["ref"], pr["head"]["sha"])
                return pr
            if seg[2:] == ["reviews"] and method == "GET":
                return self.reviews.get(n, [])
            if seg[2:] == ["reviews"] and method == "POST":
                r = {"id": self._nid(), "user": {"login": "github-actions[bot]"},
                     "state": {"COMMENT": "COMMENTED", "APPROVE": "APPROVED"}.get(
                         body.get("event", "COMMENT"), "COMMENTED"),
                     "commit_id": body.get("commit_id") or pr["head"]["sha"],
                     "body": body.get("body", "")}
                self.reviews.setdefault(n, []).append(r)
                return r
            if len(seg) == 4 and seg[2] == "reviews" and method == "PUT":
                # PUT /pulls/{n}/reviews/{id}: update own review body only.
                rid = int(seg[3])
                for r in self.reviews.get(n, []):
                    if r.get("id") == rid:
                        if "body" in body:
                            r["body"] = body["body"]
                        return r
                raise GitHubError(404, "review not found")
            if seg[2:] == ["files"] and method == "GET":
                # GitHub lists only files CHANGED vs the base branch.
                commit = self.commits.get(self.branches.get(pr["head"]["ref"], ""), {})
                tree = self.trees.get(commit.get("tree_sha", ""), {"files": {}})
                base_commit = self.commits.get(self.branches.get(pr["base"]["ref"], ""), {})
                base_tree = self.trees.get(base_commit.get("tree_sha", ""), {"files": {}})
                return [{"filename": p, "sha": s} for p, s in tree["files"].items()
                        if base_tree["files"].get(p) != s]
            if seg[2:] == ["commits"] and method == "GET":
                return self._branch_commits(pr["head"]["ref"])
        if seg == ["commits"] and method == "GET":
            sha_q = ""
            for part in query.split("&"):
                if part.startswith("sha="):
                    sha_q = part[4:]
            return self._branch_commits(sha_q)
        if seg[:2] == ["git", "ref"] and method == "GET":
            if len(seg) >= 3 and seg[2] == "tags":
                tag = "/".join(seg[3:])
                ref = self.tag_refs.get(tag)
                if ref is None:
                    raise GitHubError(404, "ref not found")
                return {"ref": f"refs/tags/{tag}",
                        "object": {"sha": ref["sha"], "type": ref["type"]}}
            branch = "/".join(seg[3:])
            if branch not in self.branches:
                raise GitHubError(404, "ref not found")
            return {"ref": f"refs/heads/{branch}",
                    "object": {"sha": self.branches[branch], "type": "commit"}}
        if seg[:2] == ["git", "tags"] and method == "GET":
            obj = self.tag_objects.get(seg[2])
            if obj is None:
                raise GitHubError(404, "tag not found")
            return obj
        if seg == ["git", "refs"] and method == "POST":
            branch = body["ref"].removeprefix("refs/heads/")
            self.branches[branch] = body["sha"]
            return {"ref": body["ref"], "object": {"sha": body["sha"]}}
        if seg[:2] == ["git", "refs"] and method == "PATCH":
            branch = "/".join(seg[3:])
            self.branches[branch] = body["sha"]
            # Keep open PR head sha in sync.
            for pr in self.pulls.values():
                if pr["head"]["ref"] == branch:
                    pr["head"]["sha"] = body["sha"]
            return {"ref": f"refs/heads/{branch}", "object": {"sha": body["sha"]}}
        if seg == ["git", "blobs"] and method == "POST":
            data = base64.b64decode(body["content"])
            bsha = hashlib.sha1(b"blob" + data).hexdigest()
            self.blobs[bsha] = data
            return {"sha": bsha}
        if seg == ["git", "trees"] and method == "POST":
            files = dict(self.trees.get(body.get("base_tree", ""), {"files": {}})["files"])
            for e in body.get("tree", []):
                files[e["path"]] = e["sha"]
            tsha = hashlib.sha1(json.dumps(files, sort_keys=True).encode()).hexdigest()
            self.trees[tsha] = {"sha": tsha, "files": files}
            return {"sha": tsha}
        if seg == ["git", "commits"] and method == "POST":
            csha = self._sha(b"commit" + str(body.get("tree", "")).encode())
            self.commits[csha] = {"sha": csha, "tree_sha": body["tree"],
                                  "parents": body.get("parents", []),
                                  "author_login": "github-actions[bot]"}
            return {"sha": csha}
        if seg[:2] == ["git", "commits"] and method == "GET":
            c = self.commits.get(seg[2])
            if c is None:
                raise GitHubError(404, "commit not found")
            return {"sha": c["sha"], "tree": {"sha": c["tree_sha"]},
                    "parents": [{"sha": p} for p in c["parents"]]}
        if seg[:1] == ["contents"] and method == "GET":
            fpath = "/".join(seg[1:])
            ref = ""
            for part in query.split("&"):
                if part.startswith("ref="):
                    ref = part[4:]
            commit_sha = self.branches.get(ref, ref)
            commit = self.commits.get(commit_sha)
            if commit is None:
                raise GitHubError(404, "ref not found")
            tree = self.trees.get(commit["tree_sha"], {"files": {}})
            bsha = tree["files"].get(fpath)
            if bsha is None:
                raise GitHubError(404, "content not found")
            data = self.blobs[bsha]
            return {"name": fpath.split("/")[-1], "path": fpath, "sha": bsha,
                    "type": "file",
                    "content": base64.b64encode(data).decode(),
                    "encoding": "base64"}
        if seg[:1] == ["releases"]:
            if len(seg) == 1 and method == "GET":
                return list(self.releases.values())
            if seg[1:] and seg[1] == "tags" and method == "GET":
                tag = seg[2]
                for rel in self.releases.values():
                    if rel["tag_name"] == tag and not rel["draft"]:
                        return rel
                raise GitHubError(404, "release not found")
            if len(seg) == 1 and method == "POST":
                rid = self._next_release
                self._next_release += 1
                rel = {"id": rid, "tag_name": body["tag_name"],
                       "name": body.get("name", ""), "draft": bool(body.get("draft")),
                       "target_commitish": body.get("target_commitish", ""),
                       "body": body.get("body", ""), "assets": [],
                       "upload_url": f"https://uploads.github.com/repos/{self.repo}/releases/{rid}/assets{{?name,label}}"}
                self.releases[rid] = rel
                if not rel["draft"]:
                    self._ensure_tag_for_release(rel)
                return rel
            rid = int(seg[1])
            rel = self.releases.get(rid)
            if rel is None:
                raise GitHubError(404, "release not found")
            if len(seg) == 2 and method == "GET":
                return rel
            if len(seg) == 2 and method == "PATCH":
                was_draft = rel.get("draft")
                rel.update({k: body[k] for k in ("draft", "name", "body", "target_commitish") if k in body})
                if was_draft and body.get("draft") is False:
                    self._ensure_tag_for_release(rel)
                return rel
            if seg[2:] == ["assets"] and method == "GET":
                return rel["assets"]
        if seg[:2] == ["actions", "workflows"] and method == "GET":
            # GitHub accepts only the workflow id or the workflow FILE
            # NAME (basename) here — a full .github/workflows/... path
            # is not a valid route and must 404 like the real API.
            basename = config.WORKFLOW_PATH.rsplit("/", 1)[-1]
            if len(seg) != 3 or seg[2] not in (str(self.workflow_id), basename):
                raise GitHubError(404, "workflow not found")
            return {"id": self.workflow_id, "path": config.WORKFLOW_PATH,
                    "name": config.WORKFLOW_NAME, "state": "active"}
        if seg[:2] == ["actions", "runs"] and method == "GET":
            run = self.runs.get(int(seg[2]))
            if run is None:
                raise GitHubError(404, "run not found")
            if len(seg) == 3:
                return run
            if seg[3:] == ["artifacts"]:
                def _wr_id(a):
                    wr = a.get("workflow_run")
                    return wr.get("id") if isinstance(wr, dict) else a.get("workflow_run_id")
                arts = [a for a in self.artifacts.values()
                        if _wr_id(a) == int(seg[2])]
                return {"total_count": len(arts), "artifacts": arts}
        if seg[:2] == ["actions", "artifacts"] and method == "GET":
            art = self.artifacts.get(int(seg[2]))
            if art is None:
                raise GitHubError(404, "artifact not found")
            return art
        raise GitHubError(404, f"unrouted {method} /{'/'.join(seg)}")

    # -- helpers mirroring GitHubClient --------------------------------------
    def get_or_none(self, path: str):
        try:
            return self.request("GET", path)
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

    def paginated(self, path: str, key: str | None = None) -> list:
        out: list = []
        page = 1
        sep = "&" if "?" in path else "?"
        while True:
            chunk = self.request("GET", f"{path}{sep}per_page=100&page={page}")
            items = chunk.get(key, []) if isinstance(chunk, dict) and key else chunk
            if not isinstance(items, list):
                items = []
            out.extend(items)
            if len(items) < 100:
                return out
            page += 1
            if page > 50:
                raise GitHubError(0, "pagination bound exceeded")

    def get_issue(self, n: int):
        return self.request("GET", f"/issues/{n}")

    def get_sub_issue_numbers(self, parent: int) -> list[int]:
        return [int(s["number"]) for s in self.paginated(f"/issues/{parent}/sub_issues", key=None)]

    def find_child(self, parent: int, candidate_id: str, fp: str):
        from . import fingerprint as _fp
        subs = set(self.get_sub_issue_numbers(parent))
        for n in sorted(subs):
            iss = self.get_issue(n)
            if (iss.get("user") or {}).get("login") not in config.BOT_LOGINS:
                continue
            for m in _fp.parse_child_markers(iss.get("body") or ""):
                if (m["parent"], m["candidate_id"], m["fingerprint"]) == (parent, candidate_id, fp):
                    return iss
        for iss in sorted(self.paginated("/issues?state=all", key=None),
                          key=lambda i: int(i.get("number", 0))):
            if int(iss.get("number", 0)) in subs:
                continue
            if (iss.get("user") or {}).get("login") not in config.BOT_LOGINS:
                continue
            for m in _fp.parse_child_markers(iss.get("body") or ""):
                if (m["parent"], m["candidate_id"], m["fingerprint"]) == (parent, candidate_id, fp):
                    return iss
        return None

    def create_issue(self, title: str, body: str):
        return self.request("POST", "/issues", {"title": title, "body": body})

    def link_sub_issue(self, parent: int, child_db_id: int):
        return self.request("POST", f"/issues/{parent}/sub_issues",
                            {"sub_issue_id": child_db_id})

    def create_pr(self, title: str, head: str, body: str):
        return self.request("POST", "/pulls",
                            {"title": title, "head": head,
                             "base": config.DEFAULT_BRANCH, "body": body})

    def review_comment(self, pr: int, body: str, commit_id: str | None = None):
        payload: dict = {"body": body, "event": "COMMENT"}
        if commit_id:
            payload["commit_id"] = commit_id
        return self.request("POST", f"/pulls/{pr}/reviews", payload)

    def update_review(self, pr: int, review_id: int, body: str):
        return self.request("PUT", f"/pulls/{pr}/reviews/{review_id}",
                            {"body": body})

    def assign(self, issue: int, login: str):
        return self.request("POST", f"/issues/{issue}/assignees",
                            {"assignees": [login]})

    def download_artifact_bytes(self, artifact_id: int) -> bytes:
        if artifact_id not in self.artifact_bytes:
            raise GitHubError(404, "artifact bytes missing")
        return self.artifact_bytes[artifact_id]

    def download_asset_bytes(self, asset_id: int) -> bytes:
        for rel in self.releases.values():
            for a in rel["assets"]:
                if a["id"] == asset_id:
                    return a["_bytes"]
        raise GitHubError(404, "asset bytes missing")

    def upload_release_asset(self, release: dict, name: str, data: bytes) -> dict:
        rel = self.releases[int(release["id"])]
        aid = self._next_asset
        self._next_asset += 1
        asset = {"id": aid, "name": name,
                 "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                 "size": len(data), "_bytes": bytes(data)}
        rel["assets"].append(asset)
        return {k: v for k, v in asset.items() if k != "_bytes"}


class FakeGitHub:
    """Legacy dict-shaped in-memory double (unit tests only).

    New lifecycle/release tests must use FakeTransport through the real
    controller functions; this class remains for small pure-helper tests.
    """

    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.prs: dict[int, dict] = {}
        self.releases: dict[str, dict] = {}
        self.comments: list[dict] = []
        self._next_issue = 100
        self._next_pr = 200
        self._db = 9000

    def create_issue(self, title: str, body: str, author: str = config.OWNER):
        n = self._next_issue
        self._next_issue += 1
        self._db += 1
        self.issues[n] = {"number": n, "id": self._db, "title": title,
                          "body": body, "state": "open", "author": author,
                          "user": {"login": author},
                          "sub_issues": [], "assignees": []}
        return self.issues[n]

    def get_sub_issue_numbers(self, parent: int):
        return list(self.issues[parent]["sub_issues"])

    def find_child(self, parent: int, candidate_id: str, fp: str):
        from . import fingerprint as _fp
        subs = set(self.issues[parent]["sub_issues"]) if parent in self.issues else set()
        for iss in self.issues.values():
            if iss["number"] not in subs:
                continue  # verify real sub-issue relationship, not labels
            for m in _fp.parse_child_markers(iss["body"]):
                if (m["parent"], m["candidate_id"], m["fingerprint"]) == (parent, candidate_id, fp):
                    return iss
        return None

    def link_sub_issue(self, parent: int, child_db_id: int):
        child = next(i for i in self.issues.values() if i["id"] == child_db_id)
        if child["number"] not in self.issues[parent]["sub_issues"]:
            self.issues[parent]["sub_issues"].append(child["number"])
        return child

    def close_issue(self, n: int, *, reason: str = "completed", by_owner: bool = True):
        self.issues[n]["state"] = "closed"
        self.issues[n]["state_reason"] = reason
        self.issues[n]["closed_by_owner"] = by_owner
        self.issues[n]["closed_by"] = {"login": config.OWNER if by_owner else "someone"}
        return self.issues[n]

    def create_or_reuse_pr(self, *, branch: str, child: int, head_sha: str,
                           artifact_key: str):
        for pr in self.prs.values():
            if pr["head_branch"] == branch:
                return pr
        n = self._next_pr
        self._next_pr += 1
        body = (f"Production bundle for child #{child}.\n\n"
                + fingerprint.receipt_marker(run_id=1, artifact_id=1, pr=n,
                                             head_sha=head_sha, child=child))
        self.prs[n] = {"number": n, "head_branch": branch, "head_sha": head_sha,
                       "base": "main", "merged": False, "merged_by": None,
                       "body": body, "reviews": [], "_artifact_key": artifact_key,
                       "_duplicate_notify": False}
        return self.prs[n]

    def post_review_comment(self, pr: int, *, passed: bool):
        body = "@YusukeIwaki please check this" if passed else "Changes requested by automated review."
        self.comments.append({"pr": pr, "body": body, "event": "COMMENT"})
        if passed:
            self.prs[pr].setdefault("assignees", []).append(config.OWNER)
        return body

    def publish_release(self, *, tag: str, pr: int, assets: dict):
        if tag in self.releases and self.releases[tag].get("assets") != assets:
            raise ValueError("mismatched existing release asset must fail")
        self.releases[tag] = {"tag": tag, "pr": pr, "assets": assets, "draft": False}
        return self.releases[tag]
