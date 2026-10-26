"""The GitHub REST calls forge_ucs_drift makes, with the standard library only.

Reads (releases, archives, refs) work without a token on public repos. Writes (a branch commit and a pull
request) go through the Git Data API, so the job needs no git binary.
"""

import base64
import json
import shutil
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


class GitHub:
    def __init__(self, token: str | None = None, api: str = API):
        self.token, self.api = token, api.rstrip("/")

    def request(self, method: str, path: str, body: dict | None = None, raw: bool = False):
        url = path if path.startswith("http") else f"{self.api}{path}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "forge-ucs-drift"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            raise GitHubError(e.code, e.read().decode(errors="replace")[:500]) from None
        if raw:
            return payload
        return json.loads(payload) if payload else None

    # ── Reads ───────────────────────────────────────────────────────────────────
    def releases(self, repo: str) -> list[dict]:
        """Published (not draft, not prerelease) releases, newest first."""
        rows = self.request("GET", f"/repos/{repo}/releases?per_page=30") or []
        return [r for r in rows if not r.get("draft") and not r.get("prerelease")]

    def branch_sha(self, repo: str, branch: str) -> str:
        return self.request("GET", f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(branch)}")["object"]["sha"]

    def tree_modes(self, repo: str, sha: str) -> dict[str, str]:
        """{path: git file mode} for every file in a commit, so a commit keeps each file's mode."""
        tree = self.request("GET", f"/repos/{repo}/git/trees/{sha}?recursive=1")
        return {e["path"]: e["mode"] for e in tree.get("tree", []) if e.get("type") == "blob"}

    def download_tarball(self, repo: str, ref: str, dest: Path) -> None:
        dest.write_bytes(self.request("GET", f"/repos/{repo}/tarball/{urllib.parse.quote(ref)}", raw=True))

    # ── Writes ──────────────────────────────────────────────────────────────────
    def commit_files(self, repo: str, branch: str, base_sha: str, files: dict[str, tuple[bytes, str] | None],
                     message: str) -> str:
        """Commit files ({path: (content, mode)}, or None to delete) on top of base_sha, and point branch at it
        (created, or force-moved). Returns the new commit's sha."""
        base_tree = self.request("GET", f"/repos/{repo}/git/commits/{base_sha}")["tree"]["sha"]
        tree = []
        for path, entry in sorted(files.items()):
            if entry is None:
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            content, mode = entry
            blob = self.request("POST", f"/repos/{repo}/git/blobs",
                                {"content": base64.b64encode(content).decode(), "encoding": "base64"})
            tree.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
        new_tree = self.request("POST", f"/repos/{repo}/git/trees", {"base_tree": base_tree, "tree": tree})["sha"]
        commit = self.request("POST", f"/repos/{repo}/git/commits",
                              {"message": message, "tree": new_tree, "parents": [base_sha]})["sha"]
        ref = f"heads/{branch}"
        try:
            self.request("PATCH", f"/repos/{repo}/git/refs/{ref}", {"sha": commit, "force": True})
        except GitHubError as e:
            if e.status not in (404, 422):
                raise
            self.request("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/{ref}", "sha": commit})
        return commit

    def branch_head_message(self, repo: str, branch: str) -> str | None:
        """The message of the branch's head commit, or None when the branch doesn't exist."""
        try:
            sha = self.branch_sha(repo, branch)
        except GitHubError as e:
            if e.status == 404:
                return None
            raise
        return self.request("GET", f"/repos/{repo}/git/commits/{sha}")["message"]

    def open_pull(self, repo: str, branch: str) -> dict | None:
        owner = repo.split("/")[0]
        pulls = self.request("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{urllib.parse.quote(branch)}") or []
        return pulls[0] if pulls else None

    def upsert_pull(self, repo: str, branch: str, base: str, title: str, body: str, draft: bool,
                    labels: list[str], comment: str) -> dict:
        """Update the open pull request from branch (and comment on it), or open one."""
        existing = self.open_pull(repo, branch)
        if existing:
            pr = self.request("PATCH", f"/repos/{repo}/pulls/{existing['number']}", {"title": title, "body": body})
            self.request("POST", f"/repos/{repo}/issues/{existing['number']}/comments", {"body": comment})
        else:
            pr = self.request("POST", f"/repos/{repo}/pulls",
                              {"title": title, "head": branch, "base": base, "body": body, "draft": draft})
        for label in labels:
            try:
                self.request("POST", f"/repos/{repo}/labels", {"name": label, "color": "c5def5"})
            except GitHubError as e:
                if e.status != 422:  # 422: it already exists
                    raise
        self.request("POST", f"/repos/{repo}/issues/{pr['number']}/labels", {"labels": labels})
        return pr


COMPLETE = ".complete"  # written last, so a folder interrupted mid-extract is never mistaken for a full one


def is_extracted(dest: Path) -> bool:
    return (dest / COMPLETE).is_file()


def extract(archive: Path, dest: Path, keep: tuple[str, ...] = ()) -> None:
    """Unpack a GitHub tarball into dest without its top-level folder, keeping only paths under keep (all if
    empty). Links, devices, and paths that escape dest are skipped. Unpacks in place (volumes may not support
    renaming folders) and writes the .complete marker last."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts[1:]
            if not parts or (keep and parts[0] not in keep) or ".." in parts or not (member.isfile() or member.isdir()):
                continue
            target = dest.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src:
                target.write_bytes(src.read())
            if member.mode & 0o111:
                try:
                    target.chmod(0o755)
                except OSError:  # volumes may not keep modes; publishing reads them from GitHub's tree instead
                    pass
    (dest / COMPLETE).write_text(archive.name + "\n")
