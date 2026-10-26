"""An upstream release's skills: each file's hash, and each skill's frontmatter."""

import hashlib
import re
from pathlib import Path

import yaml

ROOTS = ("skills", "experimental")
FRONTMATTER = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
VERSION_LINE = re.compile(rb"(?m)^\s+version:.*\n")


def frontmatter(text: str) -> dict:
    match = FRONTMATTER.match(text)
    try:
        return (yaml.safe_load(match.group(1)) or {}) if match else {}
    except yaml.YAMLError:
        return {}


def content_hash(path: Path) -> str:
    """sha256 of a file. For SKILL.md, without its frontmatter version line: a version bump alone isn't a change
    to what the skill says."""
    data = path.read_bytes()
    if path.name == "SKILL.md" and data.startswith(b"---\n"):
        head, sep, body = data[4:].partition(b"\n---")
        data = b"---\n" + VERSION_LINE.sub(b"", head + b"\n") + sep + body
    return hashlib.sha256(data).hexdigest()


def snapshot(tree: Path) -> dict[str, dict]:
    """{skill: {"dir", "experimental", "version", "description", "files": {path in the skill: sha256}}}.

    Compares by content hash: upstream doesn't reliably bump metadata.version when a skill's text changes."""
    skills = {}
    for root in ROOTS:
        base = tree / root
        if not base.is_dir():
            continue
        for skill_dir in sorted(p for p in base.iterdir() if (p / "SKILL.md").is_file()):
            meta = frontmatter((skill_dir / "SKILL.md").read_text(errors="replace"))
            version = (meta.get("metadata") or {}).get("version")
            skills[skill_dir.name] = {
                "dir": f"{root}/{skill_dir.name}",
                "experimental": root == "experimental",
                "version": str(version) if version else None,
                "description": str(meta.get("description") or ""),
                "files": {p.relative_to(skill_dir).as_posix(): content_hash(p)
                          for p in sorted(skill_dir.rglob("*")) if p.is_file()},
            }
    return skills
