# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Write <workdir>/README.md, which explains a Forge project to whoever opens its folder (every use-case skill).

    uv run <skill>/scripts/write_readme.py --state <workdir>/<state file> [--force]

Run it each time a module's Verify passes, module 00's included. The README covers every Forge use case whose state
file is in <workdir> (forge-doc-qa-agent's folder also holds its index's doc-index.json), required ones first. Each is
described from its own skill folder, a sibling of this one: the SKILL.md overview, architecture, decision points,
Done when, and resume table; the settings in its state file; and each module's purpose, inputs and outputs, steps,
explanations, and status (from `completed`). `<key>` placeholders in that text get the state file's values, except in
the architecture diagram, which stays aligned. It reads only local files, never the workspace.

The README's first line records a checksum. Its Notes section is the user's: every update keeps it. A README edited
outside Notes is kept and listed under kept_edited; --force rewrites it, still keeping Notes. A README.md that Forge
didn't write is never changed: the project's goes to FORGE-README.md instead. Prints a JSON summary. Every use-case
skill carries an identical copy (`shared` in ucs.yaml); tests/test_skills.py fails if the copies drift.
"""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

import yaml

SKILL_DIR = Path(__file__).resolve().parent.parent
README, FALLBACK = "README.md", "FORGE-README.md"
MARK = ("<!-- Written by Forge's write_readme.py from {sources} (sha256:{digest}). Forge rewrites this file after each "
        "module and keeps the Notes section: put your own text there. -->")
MARK_RE = re.compile(r"^<!-- Written by Forge's write_readme\.py .*\(sha256:([0-9a-f]{12})\)")
NOTES_START, NOTES_END = "<!-- forge:notes -->", "<!-- /forge:notes -->"
NOTES_RE = re.compile(re.escape(NOTES_START) + r"\n(.*?)" + re.escape(NOTES_END), re.DOTALL)
NOTES_DEFAULT = "Add your own notes here. Forge keeps this section when it updates the file.\n"
PLACEHOLDER = re.compile(r"<([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)>")
NOT_COPIED = {"Inputs → outputs", "Steps", "Verify", "Troubleshooting"}  # module sections summarized or left out
TREE_ENTRY = re.compile(r"^[├└]── (\S+)\s*(?:#\s?(.*))?$")
CHECKLIST = re.compile(r"(?m)^- \[[ xX]\] (\d\d) (.+)$")
CRITERION = re.compile(r"^(.*?)(?:\s*\((only [^)]*)\))?:\s+(.+)$")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/<this skill's state file>")
    p.add_argument("--force", action="store_true",
                   help="Rewrite a README that was edited by hand outside its Notes section (Notes are kept)")
    return p.parse_args()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


# ── Reading Markdown ─────────────────────────────────────────────────────────────


def fence(line: str, fenced: bool) -> bool:
    return not fenced if line.lstrip().startswith("```") else fenced


def split_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """(text before the first `## ` heading, [(title, body)]), ignoring `#` lines inside code blocks."""
    head, parts, fenced = [], [], False
    for line in text.splitlines():
        fenced = fence(line, fenced)
        if not fenced and line.startswith("## "):
            parts.append((line[3:].strip(), []))
        else:
            (parts[-1][1] if parts else head).append(line)
    return "\n".join(head).strip("\n"), [(title, "\n".join(body).strip("\n")) for title, body in parts]


def blocks(text: str) -> list[str]:
    """Paragraphs, lists, tables, and code blocks: text split on blank lines outside code blocks."""
    out, current, fenced = [], [], False
    for line in text.splitlines():
        fenced = fence(line, fenced)
        if line.strip() or fenced:
            current.append(line)
        elif current:
            out.append("\n".join(current))
            current = []
    return out + (["\n".join(current)] if current else [])


def code_blocks(text: str) -> list[str]:
    return re.findall(r"(?ms)^```[^\n]*\n(.*?)\n```", text)


def tables(text: str) -> list[list[str]]:
    found, current, fenced = [], [], False
    for line in text.splitlines() + [""]:
        fenced = fence(line, fenced)
        if not fenced and line.startswith("|"):
            current.append(line)
        elif current:
            found.append(current)
            current = []
    return found


def cells(row: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", row.strip())[1:-1]]


def table_with(text: str, header: str) -> list[str]:
    """The first table whose first header cell is `header`, or []."""
    return next((t for t in tables(text) if cells(t[0])[:1] == [header]), [])


def shift_headings(text: str, by: int) -> str:
    out, fenced = [], False
    for line in text.splitlines():
        fenced = fence(line, fenced)
        if not fenced and (match := re.match(r"^(#{1,6}) ", line)):
            line = "#" * min(6, len(match.group(1)) + by) + line[len(match.group(1)):]
        out.append(line)
    return "\n".join(out)


# ── Skills and their state ───────────────────────────────────────────────────────


def load_skill(folder: Path) -> dict:
    ucs = yaml.safe_load((folder / "ucs.yaml").read_text()) or {}
    text = (folder / "SKILL.md").read_text()
    match = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
    meta = (yaml.safe_load(match.group(1)) if match else None) or {}
    head, sections = split_sections(text[match.end():] if match else text)
    workflow = dict(sections).get("Workflow", "")
    return {
        "name": folder.name, "folder": folder, "ucs": ucs, "catalog": ucs.get("catalog") or {},
        "version": str((meta.get("metadata") or {}).get("version") or ucs.get("version") or ""),
        "head": head, "sections": dict(sections),
        "checklist": dict(CHECKLIST.findall(workflow)),
        "produces": {c[0]: c[2] for c in map(cells, table_with(workflow, "#")[2:]) if len(c) >= 3},
        "tree": next((b for b in code_blocks(workflow) if b.startswith("<workdir>/")), ""),
    }


def installed() -> dict[str, dict]:
    """The Forge use-case skills next to this one, by name: folders whose ucs.yaml catalog names a state file."""
    skills = {}
    for ucs in sorted(SKILL_DIR.parent.glob("*/ucs.yaml")):
        try:
            skill = load_skill(ucs.parent)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if skill["catalog"].get("state_file"):
            skills[skill["name"]] = skill
    return skills


def read_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"{path}: {e}") from None
    if not isinstance(state, dict):
        raise SystemExit(f"{path}: not a JSON object")
    return state


def projects(workdir: Path, skills: dict[str, dict]) -> list[dict]:
    """Each use case whose state file is in workdir, plus a required one whose state file the other's names (such as
    index_state), required ones first."""
    found = {}
    for name, skill in skills.items():
        path = workdir / skill["catalog"]["state_file"]
        if path.is_file():
            found[name] = {"skill": skill, "path": path, "state": read_state(path), "here": True, "workdir": workdir}
    for project in list(found.values()):
        for dep in project["skill"]["ucs"].get("requires") or []:
            other = skills.get(dep.get("name"))
            if not other or other["name"] in found:
                continue
            for value in project["state"].values():
                if isinstance(value, str) and Path(value).name == other["catalog"]["state_file"]:
                    path = (workdir / value).resolve()
                    if path.is_file():
                        found[other["name"]] = {"skill": other, "path": path, "state": read_state(path), "here": False,
                                                "workdir": workdir}
                        break
    ordered: list[dict] = []

    def visit(name: str, path: tuple = ()) -> None:
        if name in path or found[name] in ordered:
            return
        for dep in found[name]["skill"]["ucs"].get("requires") or []:
            if dep.get("name") in found:
                visit(dep["name"], (*path, name))
        ordered.append(found[name])

    for name in sorted(found):
        visit(name)
    return ordered


def values_for(project: dict, found: list[dict]) -> dict:
    """The values `<key>` stands for in a skill's text: its state file, then the state files of the skills it requires."""
    values = {}
    required = {dep.get("name") for dep in project["skill"]["ucs"].get("requires") or []}
    for other in found:
        if other["skill"]["name"] in required:
            values.update(other["state"])
    return values | project["state"]


def scalar(value) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and value.strip():
        return " ".join(value.split())
    return None


def lookup(values: dict, key: str):
    for part in key.split("."):
        if not isinstance(values, dict) or part not in values:
            return None
        values = values[part]
    return values


def fill(text: str, values: dict) -> str:
    """Replace each `<key>` that has a value, and `<workdir>`, which is the README's folder. Pipes are escaped in table
    rows."""
    text = re.sub(r"\b(own|same) `<workdir>`", r"\1 folder", text)
    text = re.sub(r"`<workdir>`(?!/)", "this folder", text)

    def line(row: str) -> str:
        def one(match: re.Match) -> str:
            if match.group(1) == "workdir":
                return "." if row[match.end():match.end() + 1] == "/" else "this folder"
            value = scalar(lookup(values, match.group(1)))
            if value is None:
                return match.group(0)
            return value.replace("|", "\\|") if row.startswith("|") else value
        return PLACEHOLDER.sub(one, row)
    return "\n".join(line(row) for row in text.splitlines())


def escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def names(mapping: dict) -> str:
    return ", ".join(f"`{escape(str(k))}`" for k in list(mapping)[:8]) + (", …" if len(mapping) > 8 else "")


def brief(value) -> str:
    """A state value for a table cell: short values whole, long ones summarized (the state file has them all)."""
    if value is None or value == "" or value == [] or value == {}:
        return "—"
    if isinstance(value, str):
        text = " ".join(value.split())
        if len(text) <= 80 and "`" not in text:
            return f"`{escape(text)}`"
        return escape(text if len(text) <= 300 else text[:299] + "…")
    if isinstance(value, dict) and isinstance(value.get("properties"), dict) and value["properties"]:
        return "fields " + names(value["properties"])  # a JSON schema
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= 80:
        return f"`{escape(text)}`"
    if isinstance(value, dict):
        return f"{len(value)} entries: {names(value)}"
    if isinstance(value, list):
        return f"{len(value)} items"
    return escape(text[:79] + "…")


# ── The README ───────────────────────────────────────────────────────────────────


def module_info(skill: dict, entry: dict) -> dict:
    text = (skill["folder"] / entry["file"]).read_text()
    head, sections = split_sections(text)
    lines = head.splitlines()
    match = re.match(r"^# (\d\d) · (.+)$", lines[0]) if lines else None
    steps, explained, io = [], [], ""
    for title, body in sections:
        if numbered := re.match(r"^\d+\.\s+(.+)$", title):
            steps.append(numbered.group(1))
        elif title == "Steps":  # a numbered list: each item's bold lead, or its first sentence
            for item in re.findall(r"(?m)^\d+\.\s+(.+)$", body):
                lead = re.match(r"^\*\*(.+?)\*\*", item)
                steps.append((lead.group(1) if lead else re.split(r"(?<=[.:])\s", item, maxsplit=1)[0]).rstrip(".:;, "))
        elif title == "Inputs → outputs":
            io = body
        elif title not in NOT_COPIED:
            explained.append((title, body))
    number = str(entry["id"])[:2]
    return {"number": number, "file": entry["file"], "title": match.group(2).strip() if match else entry["id"],
            "purpose": "\n".join(lines[1:]).strip(), "io": io, "steps": steps, "explained": explained,
            "criterion": skill["checklist"].get(number, ""), "produces": skill["produces"].get(number, "")}


def modules(project: dict) -> list[dict]:
    done = {str(module_id)[:2] for module_id in project["state"].get("completed") or []}
    found = [module_info(project["skill"], entry) for entry in project["skill"]["ucs"].get("modules") or []]
    following = next((m["number"] for m in found if m["number"] not in done), None)
    for module in found:
        module["status"] = "done" if module["number"] in done else "next" if module["number"] == following else "to do"
    return found


def tree_items(tree: str) -> list[list]:
    """[entry, comment, child lines] for each top-level entry of a `<workdir>/` tree."""
    items = []
    for line in tree.splitlines()[1:]:
        if match := TREE_ENTRY.match(line):
            items.append([match.group(1), (match.group(2) or "").strip(), []])
        elif items and line[:1] in ("│", " ") and line.strip():
            items[-1][2].append(line[4:])
    return items


def files_tree(found: list[dict]) -> list[str]:
    """One tree of the folder, from the trees in each skill's Workflow section. An entry two skills list is described
    by the first, which the other requires."""
    entries = [["README.md", "this file: what the project is, its settings, and each module's status", []]]
    seen = {"README.md"}
    for project in found:
        if not project["here"]:
            continue
        values = values_for(project, found)
        state_file = project["skill"]["catalog"]["state_file"]
        items = tree_items(project["skill"]["tree"]) or [[state_file, f"`{project['skill']['name']}` settings", []]]
        for entry, comment, children in items:
            if entry not in seen:
                seen.add(entry)
                entries.append([fill(entry, values), fill(comment, values), [fill(c, values) for c in children]])
    width = max(len(entry) for entry, _, _ in entries) + 2
    lines = ["./"]
    for i, (entry, comment, children) in enumerate(entries):
        last = i == len(entries) - 1
        lines.append(f"{'└── ' if last else '├── '}{entry.ljust(width)}{'# ' + comment if comment else ''}".rstrip())
        lines += [("    " if last else "│   ") + child for child in children]
    return lines


def section_lines(project: dict, found: list[dict]) -> list[str]:
    skill, state = project["skill"], project["state"]
    values = values_for(project, found)
    sections = skill["sections"]
    out = [f"## {skill['catalog'].get('title') or skill['name']}", "",
           f"_`{skill['name']}` {skill['version']}; settings in `{state_file_label(project)}`._", ""]
    for block in blocks(skill["head"]):
        if not block.startswith("# ") and not block.startswith("This is a use-case skill."):
            out += [fill(block, values), ""]

    diagram = next(iter(code_blocks(sections.get("Reference architecture", ""))), "")
    if diagram:
        out += ["### Architecture", "", "```", diagram, "```", ""]
        names = dict.fromkeys(PLACEHOLDER.findall(diagram))
        known = [f"`<{key}>` is `{escape(scalar(lookup(values, key)))}`" for key in names
                 if scalar(lookup(values, key)) is not None and key != "workdir"]
        if known:
            out += ["In this project, " + ", ".join(known) + ".", ""]

    inputs = {cells(row)[0].strip("`"): cells(row) for row in table_with(sections.get("Inputs", ""), "Input")[2:]}
    out += ["### Settings", "", f"From `{state_file_label(project)}`:", "", "| Setting | Value | About |", "|---|---|---|"]
    for key, value in state.items():
        if key != "completed":
            about = inputs.get(key, [])
            out.append(f"| `{escape(key)}` | {brief(value)} | {about[2] if len(about) > 2 else ''} |")  # as defined
    out.append("")

    found_modules = modules(project)
    done = sum(m["status"] == "done" for m in found_modules)
    out += ["### Modules", "", f"{done} of {len(found_modules)} done. Each module ends with a check that must pass "
            "before the next one starts.", "", "| # | Module | Status | Produces |", "|---|---|---|---|"]
    out += [f"| {m['number']} | {escape(m['title'])} | {m['status']} | {fill(m['produces'], values)} |"
            for m in found_modules]
    out.append("")
    for m in found_modules:
        out += [f"#### {m['number']} · {m['title']} ({m['status']})", ""]
        if m["purpose"]:
            out += [fill(m["purpose"], values), ""]
        if m["io"]:
            out += [fill(m["io"], values), ""]
        if m["steps"]:
            out += ["Steps:", "", *(f"{i}. {fill(step, values)}" for i, step in enumerate(m["steps"], 1)), ""]
        if criterion := CRITERION.match(m["criterion"]):
            only = f" ({criterion.group(2)})" if criterion.group(2) else ""
            out += [f"**Done when:** {fill(criterion.group(3), values)}{only}", ""]
        for title, body in m["explained"]:
            out += [f"##### {title}", "", fill(shift_headings(body, 3), values), ""]
        out += [f"The full steps and troubleshooting are in `{m['file']}` of the `{skill['name']}` skill.", ""]

    decisions = table_with(sections.get("Decision points", ""), "Decision")
    if decisions:
        out += ["### Decisions", "", "Each choice has a default, and a case for switching:", "",
                fill("\n".join(decisions), values), ""]
    bullets = [item for block in blocks(sections.get("Done when", "")) for item in re.split(r"(?m)^(?=- )", block)]
    done_when = [item for item in bullets if item.startswith("- ") and not item.startswith("- You ")]  # not "You told…"
    if done_when:
        out += ["### Done when", "", *(fill(b.rstrip(), values) for b in done_when), ""]
    resume = table_with(sections.get("Workflow", ""), "Situation")
    if resume:
        out += ["### Changing it later", "", "Run `/forge` in this folder, or the skill by name, and say what to change. "
                "It starts here:", "", fill("\n".join(resume), values), ""]
    return out


def state_file_label(project: dict) -> str:
    return project["path"].name if project["here"] else os.path.relpath(project["path"], project["workdir"])


def compose(found: list[dict], skills: dict[str, dict]) -> str:
    """The README without its checksum line, with an empty Notes section."""
    required = {dep.get("name") for p in found for dep in p["skill"]["ucs"].get("requires") or []}
    tops = [p for p in found if p["skill"]["name"] not in required] or found[-1:]
    if len(tops) == 1:
        name = scalar(tops[0]["state"].get("name"))
        title = tops[0]["skill"]["catalog"].get("title") or tops[0]["skill"]["name"]
        out = [f"# {title}" + (f": `{name}`" if name else ""), "",
               f"This project builds {tops[0]['skill']['catalog'].get('produces', 'a Forge use case')}."]
    else:
        out = ["# Forge project", "", f"This folder holds {len(tops)} Forge use cases: " + "; ".join(
            f"**{p['skill']['catalog'].get('title')}**, {p['skill']['catalog'].get('produces')}" for p in tops) + "."]
    built = ", then ".join(f"`{p['skill']['name']}` {p['skill']['version']}" for p in found)
    files = " and ".join(f"`{state_file_label(p)}`" for p in found)
    keeps = "which keeps its settings" if len(found) == 1 else "which keep their settings"
    out += [
        f"Forge built it with {built}, {keeps} in {files}.",
        "",
        "**To pick up where it stopped, or to change something,** run `/forge` in this folder (`@forge` in Genie Code), "
        "or name the skill. It reads the state file and starts at the first module that isn't done. Forge rewrites this "
        "README after each module.",
        "",
        "## Status",
        "",
        "| Use case | Skill | State file | Progress | Next |",
        "|---|---|---|---|---|",
    ]
    for project in found:
        listed = modules(project)
        following = next((m for m in listed if m["status"] == "next"), None)
        out.append(f"| {escape(project['skill']['catalog'].get('title', ''))} | `{project['skill']['name']}` "
                   f"{project['skill']['version']} | `{state_file_label(project)}` | "
                   f"{sum(m['status'] == 'done' for m in listed)} of {len(listed)} modules done | "
                   + (f"{following['number']} · {escape(following['title'])}" if following else "all done") + " |")
    out += ["", "## Files in this folder", "", "```", *files_tree(found), "```", ""]
    for project in found:
        out += section_lines(project, found)
    present = {p["skill"]["name"] for p in found}
    following = [skills[n] for p in tops for n in p["skill"]["catalog"].get("next") or [] if n in skills and n not in present]
    if following:
        out += ["## Next steps", "", "Forge use cases that build on this one:", ""]
        out += [f"- **{s['catalog'].get('title')}** (`{s['name']}`): {s['catalog'].get('produces')}. Run `/forge` here "
                "and ask for it." for s in following]
        out.append("")
    out += ["## Notes", "", NOTES_START, NOTES_END, ""]
    return "\n".join(out)


def blank_notes(text: str) -> str:
    return NOTES_RE.sub(lambda _: f"{NOTES_START}\n{NOTES_END}", text, count=1)


def written_by_forge(path: Path) -> bool:
    return path.is_file() and bool(MARK_RE.match(path.read_text().partition("\n")[0]))


def target(workdir: Path) -> tuple[Path, str]:
    """The file to write, and a note when it isn't README.md: a README.md Forge didn't write is the user's."""
    readme, fallback = workdir / README, workdir / FALLBACK
    if written_by_forge(readme):
        return readme, ""
    note = f"{readme} wasn't written by Forge, so it's left as is; this project's README is {fallback}"
    if written_by_forge(fallback):
        return fallback, note if readme.exists() else ""
    if not readme.exists():
        return readme, ""
    if not fallback.exists():
        return fallback, note
    raise SystemExit(f"{readme} and {fallback} weren't written by Forge. Move one aside, then run this again.")


def main() -> None:
    args = parse_args()
    skills = installed()
    own = skills.get(SKILL_DIR.name)
    if not own:
        raise SystemExit(f"{SKILL_DIR / 'ucs.yaml'} has no catalog.state_file: this isn't a Forge use-case skill")
    state_file = own["catalog"]["state_file"]
    if args.state.name != state_file:
        raise SystemExit(f"--state is {args.state.name}; {own['name']} keeps its settings in {state_file}")
    if not args.state.is_file():
        raise SystemExit(f"{args.state} not found. Module 00 writes it; run preflight first.")
    workdir = args.state.resolve().parent
    found = projects(workdir, skills)
    body = compose(found, skills)
    path, note = target(workdir)
    summary = {"readme": str(path), "covers": [p["skill"]["name"] for p in found],
               "written": [], "unchanged": [], "kept_edited": []}
    if note:
        summary["note"] = note
    notes, existing = NOTES_DEFAULT, None
    if path.is_file():
        existing = path.read_text()
        recorded = MARK_RE.match(existing.partition("\n")[0]).group(1)
        rest = existing.partition("\n")[2]
        if kept := NOTES_RE.search(rest):
            notes = kept.group(1)
        if digest(blank_notes(rest)) != recorded and not args.force:
            summary["kept_edited"].append(str(path))  # edited outside Notes: the user's text wins until --force
            print(json.dumps(summary, indent=2))
            return
    sources = ", ".join(state_file_label(p) for p in found)
    text = MARK.format(sources=sources, digest=digest(body)) + "\n" + body.replace(
        f"{NOTES_START}\n{NOTES_END}", f"{NOTES_START}\n{notes}{NOTES_END}", 1)
    if existing == text:
        summary["unchanged"].append(str(path))
    else:
        path.write_text(text)
        summary["written"].append(str(path))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
