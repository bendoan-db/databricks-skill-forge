"""Apply a proposal to a copy of the Forge repo: the model's edits, then the parts the pipeline owns (shared-file
copies, the version bump, the upstream pin). Every path is checked before anything is written.
"""

import datetime
import difflib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

ALLOWED_OUTSIDE_SKILL = ("README.md",)
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "dist"}
# Added lines in code that a reviewer should read closely: they reach the network, run commands, or touch
# credentials. Flagged in the pull request, not blocked.
RISKY = re.compile(r"\b(subprocess|os\.system|popen|eval\(|exec\(|socket|urllib|requests\.|httpx|http\.client|"
                   r"base64|os\.environ|getenv|secrets|dbutils|token|curl |wget |Invoke-WebRequest|irm )", re.IGNORECASE)
CODE_SUFFIXES = {".py", ".sh", ".ps1", ".sql", ".yml", ".yaml", ".toml"}


@dataclass
class Edit:
    path: str
    action: str  # replace | create | delete
    old: str = ""
    new: str = ""
    content: str = ""


def parse_edits(raw: list) -> list[Edit]:
    edits = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"each edit must be an object, got {item!r}")
        action = item.get("action", "replace")
        if action not in ("replace", "create", "delete"):
            raise ValueError(f"unknown edit action {action!r}")
        edits.append(Edit(str(item.get("path", "")), action, str(item.get("old", "")), str(item.get("new", "")),
                          str(item.get("content", ""))))
    return edits


def check_path(path: str, skill: str) -> str | None:
    """Why the path is refused, or None. Only the target skill's folder, plus README.md."""
    parts = PurePosixPath(path).parts
    if not path or path.startswith("/") or "\\" in path or ".." in parts:
        return f"{path!r}: use a relative path inside the repository"
    if path in ALLOWED_OUTSIDE_SKILL or (len(parts) > 2 and parts[0] == "skills" and parts[1] == skill):
        return None
    return f"{path}: outside skills/{skill}/ (and README.md)"


def apply_edits(repo: Path, skill: str, edits: list[Edit]) -> list[str]:
    """Apply the edits in order; returns the problems (the repo may be partly edited when there are any)."""
    problems = []
    for edit in edits:
        if refused := check_path(edit.path, skill):
            problems.append(refused)
            continue
        target = repo / edit.path
        if edit.action == "create":
            if target.exists():
                problems.append(f"{edit.path}: create, but the file exists (use replace)")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit.content)
        elif edit.action == "delete":
            if not target.is_file():
                problems.append(f"{edit.path}: delete, but there's no such file")
                continue
            target.unlink()
        else:
            if not target.is_file():
                problems.append(f"{edit.path}: replace, but there's no such file")
                continue
            text = target.read_text()
            count = text.count(edit.old) if edit.old else 0
            if count != 1:
                problems.append(f"{edit.path}: the text to replace appears {count} times; it must appear exactly once")
                continue
            target.write_text(text.replace(edit.old, edit.new, 1))
    return problems


def sync_shared(repo: Path, skill: str, changed: list[str]) -> list[str]:
    """Copy each changed shared file (ucs.yaml `shared`) to every other skill that carries it. Returns those paths."""
    shared_by = {}
    for ucs_path in (repo / "skills").glob("*/ucs.yaml"):
        for rel in yaml.safe_load(ucs_path.read_text()).get("shared", []):
            shared_by.setdefault(rel, []).append(ucs_path.parent.name)
    copied = []
    prefix = f"skills/{skill}/"
    for path in changed:
        rel = path[len(prefix):] if path.startswith(prefix) else None
        if rel is None or skill not in shared_by.get(rel, []):
            continue
        source = repo / path
        for other in shared_by[rel]:
            if other == skill:
                continue
            dest = repo / "skills" / other / rel
            if source.is_file():
                shutil.copyfile(source, dest)
            elif dest.exists():
                dest.unlink()
            copied.append(f"skills/{other}/{rel}")
    return copied


def bump(version: str, level: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _sub_once(path: Path, pattern: str, replacement, what: str) -> None:
    text = path.read_text()
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"{path.name}: couldn't find {what}")
    path.write_text(new)


def set_version_and_pin(repo: Path, skill: str, level: str, tag: str, today: datetime.date | None = None) -> str:
    """Bump the skill's version (SKILL.md, ucs.yaml, the README row) and move its upstream pin to tag. The forge
    router has no pin: its version is in forge.yaml."""
    folder = repo / "skills" / skill
    manifest = folder / ("ucs.yaml" if (folder / "ucs.yaml").is_file() else "forge.yaml")
    old = str(yaml.safe_load(manifest.read_text())["version"])
    new = bump(old, level)
    today = (today or datetime.date.today()).isoformat()
    _sub_once(manifest, r"^version: .*$", f"version: {new}", "the version line")
    _sub_once(folder / "SKILL.md", r'^(  version: )"[^"]+"', rf'\g<1>"{new}"', "metadata.version")
    if manifest.name == "ucs.yaml":
        note = f"# moved by forge_ucs_drift on {today}; not yet certified in a workspace"
        _sub_once(manifest, r"^upstream: ([^@\s]+)@v[0-9.]+.*$", lambda m: f"upstream: {m.group(1)}@{tag}   {note}",
                  "the upstream pin")
    readme = repo / "README.md"
    if readme.is_file():
        row = re.compile(rf"^(\| \[`{re.escape(skill)}`\]\([^)]*\) \| ){re.escape(old)}( \|)", re.MULTILINE)
        readme.write_text(row.sub(rf"\g<1>{new}\g<2>", readme.read_text(), count=1))
    return new


def tree_files(root: Path) -> dict[str, Path]:
    return {p.relative_to(root).as_posix(): p for p in root.rglob("*")
            if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(root).parts) and p.name != ".complete"}


def changed_files(base: Path, candidate: Path) -> dict[str, bytes | None]:
    """{path: new content, or None when deleted} for every file that differs."""
    old, new = tree_files(base), tree_files(candidate)
    changes = {path: p.read_bytes() for path, p in new.items()
               if path not in old or old[path].read_bytes() != p.read_bytes()}
    changes.update({path: None for path in old if path not in new})
    return dict(sorted(changes.items()))


def _lines(path: Path) -> list[str]:
    return path.read_text(errors="replace").splitlines(keepends=True) if path.is_file() else []


def unified_diff(base: Path, candidate: Path, paths: list[str]) -> str:
    out = []
    for path in paths:
        out.extend(difflib.unified_diff(_lines(base / path), _lines(candidate / path), f"a/{path}", f"b/{path}"))
    return "".join(out)


def risky_lines(diff: str) -> list[str]:
    """Added lines in code files that reach the network, run commands, or touch credentials."""
    flagged, current = [], ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++") and Path(current).suffix in CODE_SUFFIXES:
            if RISKY.search(line):
                flagged.append(f"{current}: {line[1:].strip()[:160]}")
    return flagged
