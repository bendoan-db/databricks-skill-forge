"""What the LLM sees: the Forge text that depends on a base skill, and the upstream change to it."""

import json
import re
from pathlib import Path

import yaml

from . import llm

MAX_FILE_CHARS = 40_000
OVERRIDES = re.compile(r"(?ms)^## Base-skill guidance to override\n.*?(?=^## |\Z)")


def read(path: Path, limit: int = MAX_FILE_CHARS) -> str:
    if not path.is_file():
        return "(missing)"
    text = path.read_text(errors="replace")
    return text if len(text) <= limit else text[:limit] + f"\n... cut: {len(text) - limit} more characters\n"


def modules_by_id(skill_dir: Path) -> dict[str, dict]:
    if not (skill_dir / "ucs.yaml").is_file():
        return {}
    return {m["id"]: m for m in yaml.safe_load((skill_dir / "ucs.yaml").read_text())["modules"]}


def impact_lines(impacts: list[dict]) -> str:
    return "\n".join(f"- {i['kind']}: {i['detail']} (Forge modules: {', '.join(json.loads(i['modules_json']))})"
                     for i in impacts)


def assess(skills_dir: Path, skill: str, base: str, impacts: list[dict], diffs: dict[str, str],
           new_skills: dict[str, dict]) -> str:
    """The sections for one (Forge skill, base skill) assessment."""
    skill_dir = skills_dir / skill
    modules = modules_by_id(skill_dir)
    affected = sorted({m for i in impacts for m in json.loads(i["modules_json"])})
    sections = [("Upstream changes", impact_lines(impacts))]
    sections += [(f"Diff: {base}/{path}", diff or "(no text diff)") for path, diff in diffs.items()]
    if any(i["kind"] == "skill_removed" for i in impacts):
        listing = "\n".join(f"- {name}: {info['description'][:200]}" for name, info in sorted(new_skills.items()))
        sections.append(("Skills in the newer release (look for a rename)", listing))
    for module_id in affected:
        if module_id in modules:
            module = modules[module_id]
            entry = [d for d in module.get("base_skills", []) if d["name"] == base]
            sections.append((f"Forge module {module_id} ({module['file']})", read(skill_dir / module["file"])))
            sections.append((f"ucs.yaml: {module_id}'s entry for {base}", yaml.safe_dump(entry, sort_keys=False)))
        elif module_id == "coverage_gaps":
            ucs = yaml.safe_load((skill_dir / "ucs.yaml").read_text())
            gaps = [g for g in ucs.get("coverage_gaps", []) if base in str(g.get("stale_base_guidance", ""))]
            overrides = OVERRIDES.search((skill_dir / "SKILL.md").read_text())
            sections.append(("ucs.yaml coverage_gaps that name this file", yaml.safe_dump(gaps, sort_keys=False)))
            sections.append(("SKILL.md: Base-skill guidance to override", overrides.group(0) if overrides else "(none)"))
        elif module_id == "router":
            sections.append(("The forge router (skills/forge/SKILL.md)", read(skills_dir / "forge" / "SKILL.md")))
    return llm.fit(sections)


def propose(repo: Path, skill: str, impacts: list[dict], assessments: list[dict], diffs: dict[str, str],
            new_files: dict[str, str], feedback: list[str]) -> tuple[str, str]:
    """(feedback, context) for one Forge skill's edit."""
    skill_dir = repo / "skills" / skill
    sections = [("Upstream changes", impact_lines(impacts)),
                ("Assessments", json.dumps([{"base_skill": a["base_skill"], "materiality": a["materiality"],
                                             **json.loads(a["assessment_json"] or "{}")} for a in assessments], indent=1))]
    sections += [(f"Diff: {name}", diff or "(no text diff)") for name, diff in diffs.items()]
    sections += [(f"Upstream file at the newer release: {name}", text) for name, text in new_files.items()]
    files = [skill_dir / "SKILL.md"]
    if (skill_dir / "ucs.yaml").is_file():
        files.append(skill_dir / "ucs.yaml")
        affected = {m for i in impacts for m in json.loads(i["modules_json"])}
        for module_id, module in modules_by_id(skill_dir).items():
            files.append(skill_dir / module["file"])
            if module_id in affected:
                files += [skill_dir / s for s in module.get("scripts", [])]
    else:  # the router
        files += [skill_dir / "forge.yaml", skill_dir / "evals" / "routing.yaml"]
    seen = set()
    for path in files:
        if path not in seen:
            seen.add(path)
            sections.append((f"Forge file: {path.relative_to(repo).as_posix()}", read(path)))
    sections.append(("README.md", read(repo / "README.md")))
    note = ""
    if feedback:
        note = ("A previous attempt failed these checks. Fix them, starting again from the files as shown below "
                "(the previous edits were discarded):\n" + "\n".join(f"- {f}" for f in feedback))
    return note, llm.fit(sections)
