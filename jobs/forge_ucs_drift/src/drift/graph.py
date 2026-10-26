"""What each Forge skill depends on: base skills, and the files in them its modules read.

Sources, all in the Forge repo:
- each use-case skill's ucs.yaml: modules[].base_skills, the upstream pin, coverage_gaps;
- each module's **Load:** lines, which name a base skill and usually one reference file;
- the forge router's fallback table, which names base skills it points requests to.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

LOAD_LINE = re.compile(r"\*\*[Ll]oad:?\*\*")
CITED_FILE = re.compile(r"`(databricks-[a-z0-9-]+)`\s+`(references/[^`#\s]+)")
NAMED = re.compile(r"`(databricks-[a-z0-9-]+)`(\s+`references/[^`]+`)?")
GAP_FILE = re.compile(r"(databricks-[a-z0-9-]+)/((?:references/)?[\w./-]+\.md)")
PIN = re.compile(r"@(v\d+\.\d+\.\d+)")
FALLBACK_SECTION = re.compile(r"(?ms)^## \d+\. No matching use case\n(.*?)(?=^## )")
SKILL_MD = "SKILL.md"


@dataclass(frozen=True)
class Edge:
    forge_skill: str
    module: str  # a module id, "coverage_gaps", or "router"
    base_skill: str
    base_version: str  # what the module was written against; "" when it doesn't say
    cited_file: str  # SKILL.md, or a path inside the base skill such as references/1-task-functions.md


@dataclass
class ForgeSkill:
    name: str
    version: str
    pin: str  # upstream tag, such as v0.2.29
    upstream_min: str
    ucs: dict
    path: Path


def forge_skills(skills_dir: Path) -> dict[str, ForgeSkill]:
    """Every use-case skill (a folder with ucs.yaml)."""
    found = {}
    for path in sorted(p for p in skills_dir.iterdir() if (p / "ucs.yaml").is_file()):
        ucs = yaml.safe_load((path / "ucs.yaml").read_text())
        pin = PIN.search(str(ucs.get("upstream", "")))
        if not pin:
            raise ValueError(f"{path.name}/ucs.yaml: upstream must name a tag, "
                             "such as databricks/databricks-agent-skills@v0.2.29")
        found[path.name] = ForgeSkill(path.name, str(ucs["version"]), pin.group(1), str(ucs.get("upstream_min", "")), ucs, path)
    return found


def cited_files(module_text: str) -> set[tuple[str, str]]:
    """(base skill, reference file) pairs named on Load lines, anchors dropped. Same rule as check_base_skills.py."""
    return {m.groups() for line in module_text.splitlines() if LOAD_LINE.search(line) for m in CITED_FILE.finditer(line)}


def read_files(module_text: str) -> set[tuple[str, str]]:
    """(base skill, file) pairs a module tells the agent to read: the named reference file, or SKILL.md when a
    Load line names the skill alone. A Load line that names a file means "read only that file"."""
    reads = set(cited_files(module_text))
    for line in module_text.splitlines():
        if LOAD_LINE.search(line):
            reads |= {(m.group(1), SKILL_MD) for m in NAMED.finditer(line) if not m.group(2)}
    return reads


def edges(skills_dir: Path) -> list[Edge]:
    found: set[Edge] = set()
    for skill in forge_skills(skills_dir).values():
        all_deps: dict[str, str] = {}
        for module in skill.ucs["modules"]:
            deps = {d["name"]: str(d["version"]) for d in module.get("base_skills", [])}
            all_deps.update(deps)
            for name, ref in read_files((skill.path / module["file"]).read_text()):
                if name in deps:
                    found.add(Edge(skill.name, module["id"], name, deps[name], ref))
        # A coverage gap names the stale upstream file; upstream changing it may mean the gap is fixed.
        for gap in skill.ucs.get("coverage_gaps", []):
            for name, ref in GAP_FILE.findall(str(gap.get("stale_base_guidance") or "")):
                found.add(Edge(skill.name, "coverage_gaps", name, all_deps.get(name, ""), ref))
    # The router needs the base skills it points to (fallback table) and competes with (its description) to
    # exist and keep their purpose; it doesn't read their contents.
    router = skills_dir / "forge" / SKILL_MD
    if router.is_file():
        text = router.read_text()
        section = FALLBACK_SECTION.search(text)
        description = re.search(r'(?m)^description: "(.*)"$', text)
        named = re.findall(r"`(databricks-[a-z0-9-]+)`", section.group(1) if section else "")
        named += re.findall(r"\b(databricks-[a-z0-9-]+)\b", description.group(1) if description else "")
        for name in sorted(set(named)):
            found.add(Edge("forge", "router", name, "", SKILL_MD))
    return sorted(found, key=lambda e: (e.forge_skill, e.base_skill, e.module, e.cited_file))
