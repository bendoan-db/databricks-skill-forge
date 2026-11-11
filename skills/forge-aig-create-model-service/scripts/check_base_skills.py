# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Check that the base skills a use-case skill depends on are installed next to it (module 00).

Reads every base skill listed in ucs.yaml, plus the use-case skills under `requires` (ones this skill
runs first), and compares each with that skill's SKILL.md in the skills directory (by default, the
folder that holds this skill). Exits 1 when a skill is missing or older than the version the modules
were written against, because the modules cite reference files those versions don't have. Prints a
JSON report either way. It also checks that every reference file a module's **Load:** lines cite exists in
the installed base skill, because a renamed or removed file leaves that step with nothing to read. Every
use-case skill carries an identical copy; tests/test_skills.py fails if the copies drift.

    uv run <skill>/scripts/check_base_skills.py [--skills-dir <dir>]
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

SKILL_DIR = Path(__file__).resolve().parent.parent
LOAD_LINE = re.compile(r"\*\*[Ll]oad:?\*\*")
CITED_FILE = re.compile(r"`(databricks-[a-z0-9-]+)`\s+`(references/[^`#\s]+)")


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def installed_version(skill_md: Path) -> str | None:
    match = re.match(r"^---\n(.*?)\n---", skill_md.read_text(), re.DOTALL)
    meta = (yaml.safe_load(match.group(1)) if match else None) or {}
    version = (meta.get("metadata") or {}).get("version")
    return str(version) if version else None


def cited_files(module_text: str) -> set[tuple[str, str]]:
    """(base skill, reference file) pairs named on a module's Load lines, anchors dropped."""
    return {m.groups() for line in module_text.splitlines() if LOAD_LINE.search(line) for m in CITED_FILE.finditer(line)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--skills-dir", type=Path, default=SKILL_DIR.parent, help="Folder holding the base skills")
    args = p.parse_args()

    ucs = yaml.safe_load((SKILL_DIR / "ucs.yaml").read_text())
    required: dict[str, tuple[str, list[str]]] = {}
    for module in ucs["modules"]:
        for dep in module.get("base_skills", []):
            required.setdefault(dep["name"], (str(dep["version"]), []))[1].append(module["id"])
    use_case = {dep["name"] for dep in ucs.get("requires", [])}
    for dep in ucs.get("requires", []):
        required.setdefault(dep["name"], (str(dep["version"]), []))[1].append("requires")

    rows, blocking = [], []
    for name, (wanted, modules) in sorted(required.items()):
        skill_md = args.skills_dir / name / "SKILL.md"
        found = installed_version(skill_md) if skill_md.is_file() else None
        if not skill_md.is_file():
            status = "missing"
        elif wanted == "unversioned" or found is None:
            status = "present"  # nothing to compare; presence is what matters
        elif version_key(found) < version_key(wanted):
            status = "older"
        else:
            status = "ok" if version_key(found) == version_key(wanted) else "newer"
        if status in ("missing", "older"):
            blocking.append(name)
        rows.append({"skill": name, "kind": "use-case" if name in use_case else "base", "status": status,
                     "installed": found, "written_against": wanted, "modules": modules})

    missing_files = sorted({
        f"{skill}/{ref}" for module in ucs["modules"]
        for skill, ref in cited_files((SKILL_DIR / module["file"]).read_text())
        if (args.skills_dir / skill / "SKILL.md").is_file() and not (args.skills_dir / skill / ref).is_file()
    })
    print(json.dumps({"skills_dir": str(args.skills_dir.resolve()), "skills": rows, "missing_references": missing_files},
                     indent=2))
    if blocking or missing_files:
        problems = []
        if base := [name for name in blocking if name not in use_case]:
            problems.append(f"Base skills missing or too old: {base}. Install or update databricks-agent-skills "
                            f"{ucs.get('upstream_min', '')} or later")
        if forge := [name for name in blocking if name in use_case]:
            problems.append(f"Use-case skills missing or too old: {forge}. Install them from databricks-skill-forge")
        if missing_files:
            problems.append(f"Reference files the modules cite are missing: {missing_files}. The base skills changed "
                            "under this skill; update it (or its base skills)")
        sys.exit("; ".join(problems) + f" in {args.skills_dir.resolve()}, then rerun this check.")


if __name__ == "__main__":
    main()
