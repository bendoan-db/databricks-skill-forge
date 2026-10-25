# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Render forge's Use cases and Resume tables from each skill's ucs.yaml `catalog` block and forge.yaml.

    uv run tools/build_forge_catalog.py           # rewrite the block in skills/forge/SKILL.md
    uv run tools/build_forge_catalog.py --check   # exit 1 if it's out of date (tests/test_forge.py runs the same check)

Maintainer tool: it lives outside skills/, so it isn't published with any skill.
"""

import argparse
import re
import sys
from pathlib import Path

import yaml

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
FORGE = SKILLS_DIR / "forge"
BLOCK_RE = re.compile(r"(?s)(<!-- >>> catalog:[^\n]*-->\n).*?(<!-- <<< catalog -->)")
SKILL_NAME_RE = re.compile(r"(?<![`\w-])(forge-[a-z-]+[a-z])(?![`\w-])")
FIELDS = ("title", "choose_when", "not_for", "produces", "state_file", "next")


def cell(text: str) -> str:
    """One table cell: skill names in backticks, pipes escaped."""
    return SKILL_NAME_RE.sub(r"`\1`", str(text)).replace("|", "\\|")


def load(skills_dir: Path = SKILLS_DIR) -> tuple[dict, list[tuple[dict, dict]]]:
    """forge.yaml, and (use_cases entry, that skill's ucs.yaml) pairs in forge.yaml order."""
    forge = yaml.safe_load((skills_dir / "forge" / "forge.yaml").read_text())
    pairs = []
    for entry in forge["use_cases"]:
        ucs = yaml.safe_load((skills_dir / entry["name"] / "ucs.yaml").read_text())
        if missing := [field for field in FIELDS if field not in (ucs.get("catalog") or {})]:
            raise SystemExit(f"{entry['name']}/ucs.yaml catalog is missing {missing}")
        pairs.append((entry, ucs))
    return forge, pairs


def render(skills_dir: Path = SKILLS_DIR) -> str:
    _, pairs = load(skills_dir)
    lines = [
        "| Use case | Skill (minimum version) | Choose it when the user wants | Not for | Produces | Next step |",
        "|---|---|---|---|---|---|",
    ]
    for entry, ucs in pairs:
        c = ucs["catalog"]
        next_step = ", ".join(f"`{name}`" for name in c["next"]) or "—"
        lines.append(f"| {cell(c['title'])} | `{entry['name']}` ({entry['min_version']}) | {cell(c['choose_when'])} "
                     f"| {cell(c['not_for'])} | {cell(c['produces'])} | {next_step} |")
    lines += ["", "Resume table, checked in this order:", "", "| State file in `<workdir>` | Skill |", "|---|---|"]
    lines += [f"| `{ucs['catalog']['state_file']}` | `{entry['name']}` |" for entry, ucs in pairs]
    return "\n".join(lines) + "\n"


def update(text: str, block: str) -> str:
    if len(BLOCK_RE.findall(text)) != 1:
        raise SystemExit("skills/forge/SKILL.md needs exactly one catalog block between its >>> and <<< markers")
    return BLOCK_RE.sub(lambda m: m.group(1) + block + m.group(2), text)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="Exit 1 if SKILL.md is out of date; don't write")
    args = p.parse_args()
    path = FORGE / "SKILL.md"
    text = path.read_text()
    new = update(text, render())
    if args.check:
        if new != text:
            sys.exit("skills/forge/SKILL.md catalog is out of date: run `uv run tools/build_forge_catalog.py`")
        print("catalog up to date")
    elif new != text:
        path.write_text(new)
        print(f"updated {path}")
    else:
        print("catalog up to date")


if __name__ == "__main__":
    main()
