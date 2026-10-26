# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Build a Forge release: the skills archive, version-stamped installers, checksums, and release notes.

    uv run tools/build_release.py v0.1.0              # writes dist/
    uv run tools/build_release.py v0.1.0 --out DIR

The tag must be the VERSION file's semver with a leading v. The archive holds skills/ only, built the same
way every time (sorted entries, fixed timestamps and owners, no caches), so a commit always produces the
same checksum. The installers, and the Genie Code notebook, get the tag and that checksum written in, which
is how a downloaded installer finds and verifies its skills. Maintainer tool; .github/workflows/release.yml runs it on tag pushes.
"""

import argparse
import gzip
import hashlib
import io
import re
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SKIP = {"__pycache__", ".ruff_cache", ".pytest_cache", ".DS_Store"}
ARCHIVE_ROOT = "forge-skills"
MTIME = 315532800  # 1980-01-01: fixed, and recent enough that no tar warns about it
# Each installer's stamp lines: (pattern, replacement), each matched exactly once.
STAMPS = {
    "install.sh": [(r'(?m)^FORGE_VERSION=""', 'FORGE_VERSION="{tag}"'),
                   (r'(?m)^FORGE_SKILLS_SHA256=""', 'FORGE_SKILLS_SHA256="{sha}"')],
    "install.ps1": [(r"(?m)^\$ForgeVersion = ''", "$ForgeVersion = '{tag}'"),
                    (r"(?m)^\$ForgeSkillsSha256 = ''", "$ForgeSkillsSha256 = '{sha}'")],
    "install_genie_code.py": [(r'(?m)^FORGE_VERSION = ""', 'FORGE_VERSION = "{tag}"'),
                              (r'(?m)^FORGE_SKILLS_SHA256 = ""', 'FORGE_SKILLS_SHA256 = "{sha}"')],
}
REPO = "bendoan-db/databricks-skill-forge"


def check_tag(tag: str, root: Path = ROOT) -> None:
    if not TAG_RE.match(tag):
        raise SystemExit(f"{tag!r} isn't a release tag: use semver with a leading v, such as v0.1.0")
    version = (root / "VERSION").read_text().strip()
    if tag != f"v{version}":
        raise SystemExit(f"Tag {tag} doesn't match VERSION ({version}): bump VERSION, or tag v{version}")


def skill_files(skills_dir: Path) -> list[Path]:
    """Every file and folder under skills/, minus caches, in a stable order."""
    paths = []
    for path in sorted(skills_dir.rglob("*")):
        rel = path.relative_to(skills_dir)
        if any(part in SKIP for part in rel.parts) or path.suffix == ".pyc":
            continue
        paths.append(path)
    return paths


def build_archive(skills_dir: Path) -> bytes:
    """The skills folder as a .tar.gz whose bytes depend only on the files' names, contents, and exec bits."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in [skills_dir, *skill_files(skills_dir)]:
            rel = path.relative_to(skills_dir)
            info = tarfile.TarInfo(f"{ARCHIVE_ROOT}/{rel.as_posix()}" if rel.parts else ARCHIVE_ROOT)
            info.mtime, info.uid, info.gid, info.uname, info.gname = MTIME, 0, 0, "", ""
            if path.is_dir():
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            else:
                data = path.read_bytes()
                info.size, info.mode = len(data), 0o755 if path.stat().st_mode & 0o111 else 0o644
                tar.addfile(info, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0) as gz:
        gz.write(raw.getvalue())
    return compressed.getvalue()


def stamp(text: str, name: str, tag: str, sha: str) -> str:
    for pattern, replacement in STAMPS[name]:
        text, count = re.subn(pattern, replacement.format(tag=tag, sha=sha), text)
        if count != 1:
            raise SystemExit(f"{name}: expected one line matching {pattern!r} to stamp, found {count}")
    return text


def skill_versions(skills_dir: Path) -> list[tuple[str, str]]:
    rows = []
    for skill in sorted(p for p in skills_dir.iterdir() if (p / "SKILL.md").is_file()):
        match = re.search(r'(?m)^  version: "([^"]+)"', (skill / "SKILL.md").read_text())
        rows.append((skill.name, match.group(1) if match else "?"))
    return rows


def ai_dev_kit_pin(root: Path = ROOT) -> str:
    match = re.search(r'(?m)^AIDEVKIT_REF="([^"]+)"', (root / "install.sh").read_text())
    return match.group(1) if match else "?"


def release_notes(tag: str, skills_dir: Path, root: Path = ROOT) -> str:
    base = f"https://github.com/{REPO}/releases/download/{tag}"
    rows = "\n".join(f"| `{name}` | {version} |" for name, version in skill_versions(skills_dir))
    return f"""## Install

macOS and Linux:

```bash
bash <(curl -fsSL {base}/install.sh)
```

Windows (PowerShell):

```powershell
irm {base}/install.ps1 | iex
```

Both install every stable Databricks base skill and the Databricks MCP server through AI Dev Kit {ai_dev_kit_pin(root)},
then the skills below. The installers verify each download against a checksum before installing anything.
Run either with `--help` for options.

Genie Code: import {base}/install_genie_code.py into your workspace as a notebook, and run it. It puts the base
skills and the skills below in your Genie Code skills folder.

## Skills

| Skill | Version |
|---|---|
{rows}
"""


def build(tag: str, out: Path, root: Path = ROOT) -> dict[str, str]:
    """Write the release files to out; return {file name: sha256}."""
    check_tag(tag, root)
    skills_dir = root / "skills"
    out.mkdir(parents=True, exist_ok=True)
    archive = build_archive(skills_dir)
    sha = hashlib.sha256(archive).hexdigest()
    files = {f"forge-skills-{tag}.tar.gz": archive}
    for name in STAMPS:
        files[name] = stamp((root / name).read_text(), name, tag, sha).encode()
    sums = {}
    for name, data in files.items():
        (out / name).write_bytes(data)
        if name == "install.sh":
            (out / name).chmod(0o755)
        sums[name] = hashlib.sha256(data).hexdigest()
    (out / "SHA256SUMS").write_text("".join(f"{sums[name]}  {name}\n" for name in sorted(sums)))
    (out / "RELEASE_NOTES.md").write_text(release_notes(tag, skills_dir, root))
    return sums


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("tag", help="Release tag, such as v0.1.0; must match VERSION")
    p.add_argument("--out", type=Path, default=ROOT / "dist", help="Output folder (default: dist/)")
    args = p.parse_args()
    sums = build(args.tag, args.out)
    for name, sha in sorted(sums.items()):
        print(f"{sha}  {args.out / name}")


if __name__ == "__main__":
    sys.exit(main())
