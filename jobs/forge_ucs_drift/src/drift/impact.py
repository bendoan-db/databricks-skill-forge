"""Which upstream changes touch what a Forge skill depends on. Deterministic: no LLM.

Each Forge skill is compared from its own pin to the newest release, on the files its modules read only.
"""

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from .graph import SKILL_MD, Edge

# kind: severity before any LLM looks at it
SEVERITY = {
    "skill_removed": "breaking",        # a base skill a module loads is gone (or renamed)
    "cited_file_removed": "breaking",   # a Load line now points at nothing
    "cited_file_changed": "candidate",  # a reference file a module reads changed
    "skill_md_changed": "candidate",    # the base skill's SKILL.md changed
    "gap_file_changed": "candidate",    # the file a coverage gap calls stale changed: upstream may have fixed it
    "description_changed": "candidate", # the router routes by, or points to, this skill, and its purpose moved
    "version_changed": "compatible",    # frontmatter version moved, content the skill reads didn't
}
RANK = {"none": 0, "compatible": 1, "candidate": 2, "breaking": 3}
MAX_DIFF_LINES = 400


@dataclass
class Impact:
    forge_skill: str
    base_skill: str
    kind: str
    paths: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def severity(self) -> str:
        return SEVERITY[self.kind]


def impacts(skill_edges: list[Edge], old: dict, new: dict) -> list[Impact]:
    """Impacts on one Forge skill between two snapshots (snapshot.snapshot output)."""
    by_base: dict[str, list[Edge]] = {}
    for edge in skill_edges:
        by_base.setdefault(edge.base_skill, []).append(edge)
    found = []
    for base, base_edges in sorted(by_base.items()):
        forge_skill = base_edges[0].forge_skill
        modules = sorted({e.module for e in base_edges})
        if base not in new:
            found.append(Impact(forge_skill, base, "skill_removed", modules=modules,
                                detail=f"{base} isn't in the newer release"))
            continue
        if base not in old:  # the pin predates the skill: nothing to compare against
            continue
        if modules == ["router"]:  # the router needs the skill to exist and keep its purpose, not its text
            if old[base]["description"] != new[base]["description"]:
                found.append(Impact(forge_skill, base, "description_changed", [SKILL_MD], modules,
                                    f"{base}'s description changed"))
            continue
        old_files, new_files = old[base]["files"], new[base]["files"]
        for cited in sorted({e.cited_file for e in base_edges}):
            cited_modules = sorted({e.module for e in base_edges if e.cited_file == cited})
            if cited not in new_files:
                if cited in old_files:
                    found.append(Impact(forge_skill, base, "cited_file_removed", [cited], cited_modules,
                                        f"{base}/{cited} was removed or renamed"))
                continue
            if old_files.get(cited) == new_files[cited]:
                continue
            if cited_modules == ["coverage_gaps"]:
                kind = "gap_file_changed"
            else:
                kind = "skill_md_changed" if cited == SKILL_MD else "cited_file_changed"
            found.append(Impact(forge_skill, base, kind, [cited], cited_modules, f"{base}/{cited} changed"))
        if old[base]["version"] != new[base]["version"] and not any(i.base_skill == base for i in found):
            found.append(Impact(forge_skill, base, "version_changed", modules=modules,
                                detail=f"{base} {old[base]['version']} -> {new[base]['version']}"))
    return found


def severity(found: list[Impact]) -> str:
    """The worst severity among a skill's impacts, or none."""
    return max((i.severity for i in found), key=RANK.get, default="none")


def file_diff(old_tree: Path, new_tree: Path, rel: str, old_tag: str, new_tag: str) -> str:
    """A unified diff of one upstream file between two trees, cut to MAX_DIFF_LINES."""
    def lines(path: Path) -> list[str]:
        return path.read_text(errors="replace").splitlines(keepends=True) if path.is_file() else []

    diff = list(difflib.unified_diff(lines(old_tree / rel), lines(new_tree / rel), f"{rel}@{old_tag}", f"{rel}@{new_tag}"))
    if len(diff) > MAX_DIFF_LINES:
        diff = diff[:MAX_DIFF_LINES] + [f"... {len(diff) - MAX_DIFF_LINES} more diff lines cut\n"]
    return "".join(diff)
