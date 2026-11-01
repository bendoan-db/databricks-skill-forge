# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Write a Forge use-case skill's folder from the blueprint in smith.json (forge-smith module 04).

    uv run <smith>/scripts/scaffold.py --state <workdir>/smith.json [--skills-dir <dir>] [--out <dir>]

Writes <out>/<name>/ (default <out>: the skills folder holding forge-smith): SKILL.md, modules/, ucs.yaml,
evals/scenarios.yaml, scripts/check_base_skills.py, scripts/evaluate.py, and a stub for every other script a
module lists. Everything the blueprint settles is filled in: frontmatter, the Inputs table, module 00's
discovery questions, checks, permissions, and cost list, each module's Load lines (one per base skill), the
Workflow table, ucs.yaml with the installed base-skill versions, and the thresholds. What only the agent can
write is left as a `smith:` marker; check_skill.py fails until none are left. Refuses to write into an existing
folder. The blueprint format is in references/blueprint.md.
"""

import argparse
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import yaml

SMITH_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = SMITH_DIR / "templates"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MODULE_RE = re.compile(r"^(\d\d)-[a-z0-9]+(-[a-z0-9]+)*$")
STATE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.json$")
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
INPUT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
MARK = "<!-- smith: {} -->"


class BlueprintError(Exception):
    pass


def cell(text) -> str:
    """Text for a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def installed_version(skill_md: Path) -> str:
    match = re.match(r"^---\n(.*?)\n---", skill_md.read_text(), re.DOTALL)
    meta = (yaml.safe_load(match.group(1)) if match else None) or {}
    version = (meta.get("metadata") or {}).get("version")
    return str(version) if version else "unversioned"


def check_blueprint(bp: dict, skills_dir: Path) -> list[str]:
    """Every problem with the blueprint, checked before anything is written."""
    problems = []
    for key in ("name", "title", "summary", "description", "state_file", "upstream", "catalog", "inputs", "modules",
                "thresholds"):
        if not bp.get(key):
            problems.append(f"{key} is missing")
    if problems:
        return problems
    if not NAME_RE.match(bp["name"]) or len(bp["name"]) > 64:
        problems.append(f"name {bp['name']!r}: lowercase letters, digits, and hyphens, up to 64 characters")
    if len(bp["description"]) > 1024:
        problems.append(f"description is {len(bp['description'])} characters; the limit is 1024")
    if not STATE_RE.match(bp["state_file"]):
        problems.append(f"state_file {bp['state_file']!r}: a lowercase .json file name, such as claims.json")
    if not TAG_RE.match(bp["upstream"]):
        problems.append(f"upstream {bp['upstream']!r}: a databricks-agent-skills tag, such as v0.2.35")
    for key in ("title", "choose_when", "not_for", "produces"):
        if not bp["catalog"].get(key):
            problems.append(f"catalog.{key} is missing")
    required = bp["inputs"].get("required") or []
    if not required:
        problems.append("inputs.required is empty: a use case asks at least where its data is")
    names = [i.get("name") for i in required + (bp["inputs"].get("optional") or [])]
    for item in required:
        if not item.get("ask"):
            problems.append(f"input {item.get('name')}: required inputs need `ask`, the question module 00 asks")
    for name in names:
        if not name or not INPUT_RE.match(name):
            problems.append(f"input name {name!r}: snake_case")
    if len(set(names)) != len(names):
        problems.append("input names repeat")
    modules = bp["modules"]
    ids = [m.get("id", "") for m in modules]
    if not ids or ids[0] != "00-preflight":
        problems.append("the first module must be 00-preflight")
    if ids != sorted(ids) or len({i[:2] for i in ids}) != len(ids):
        problems.append(f"module ids must be unique and numbered in order: {ids}")
    for module_id in ids:
        if not MODULE_RE.match(module_id):
            problems.append(f"module id {module_id!r}: NN-name, such as 01-ingest")
    if not any(i.endswith("-evaluate") for i in ids):
        problems.append("one module must be an evaluate module (id ending in -evaluate): its thresholds define done")
    for module in modules:
        if module.get("id") != "00-preflight" and not (module.get("title") and module.get("produces")):
            problems.append(f"module {module.get('id')}: needs title and produces")
        for script in module.get("scripts", []):
            if not re.match(r"^scripts/[\w.-]+$", script):
                problems.append(f"module {module.get('id')}: script {script!r} must be scripts/<file>")
        for dep in module.get("base_skills", []):
            folder = skills_dir / str(dep.get("name"))
            if not (folder / "SKILL.md").is_file():
                problems.append(f"module {module.get('id')}: base skill {dep.get('name')} isn't installed in {skills_dir}")
            elif dep.get("reference") and not (folder / dep["reference"]).is_file():
                problems.append(f"module {module.get('id')}: {dep['name']} has no {dep['reference']}")
    first = modules[0] if modules else {}
    if "databricks-core" not in {d.get("name") for d in first.get("base_skills", [])}:
        problems.append("00-preflight must load databricks-core (profile selection and auth)")
    for metric, floor in bp["thresholds"].items():
        if not isinstance(floor, (int, float)):
            problems.append(f"threshold {metric}: a number")
    return problems


def load_line(dep: dict) -> str:
    uses = f" ({', '.join(dep['uses'])})" if dep.get("uses") else ""
    ref = f" `{dep['reference']}`" if dep.get("reference") else ""
    return f"**Load:** `{dep['name']}`{ref}{uses}."


def skill_md(bp: dict) -> str:
    state, routed = bp["state_file"], bp.get("routed", False)
    required, optional = bp["inputs"]["required"], bp["inputs"].get("optional") or []
    if "profile" not in {i["name"] for i in required + optional}:
        optional = [{"name": "profile", "default": "from `databricks-core`", "notes": "CLI profile for every command"},
                    *optional]
    lines = [
        "---",
        f"name: {bp['name']}",
        f"description: {json.dumps(bp['description'], ensure_ascii=False)}",
        "compatibility: Requires databricks CLI (>= v1.11.0), Python 3.11+, uv, and the databricks-agent-skills base "
        f"skills (>= {bp['upstream']}) installed in the same skills directory as this skill",
        "metadata:",
        '  version: "0.1.0"',
        "  forge_kind: use-case",
        "---",
        "",
        f"# {bp['title']}",
        "",
        bp["summary"].strip(),
        "",
        "This is a use-case skill. It doesn't restate platform details: each module step names the base skill (from "
        "databricks-agent-skills) to load for those details, at the point where it's needed.",
        "",
        "## Reference architecture",
        "",
        "```",
        MARK.format("the flow from the inputs to what this skill produces, with each module's tables, jobs, and endpoints"),
        "```",
        "",
        "## Conventions",
        "",
        "- `<skill>` is the folder that holds this SKILL.md. The base skills are its siblings, for example "
        "`<skill>/../databricks-core`.",
        "- This is a Forge use case. The `forge` skill (`<skill>/../forge`) usually starts it and passes along what the "
        "user already said. Loaded directly, it runs the same way." if routed else
        "- This skill was built with `forge-smith`. Run it by name: `forge` doesn't route to it unless it joins Forge's "
        "catalog.",
        f"- `<workdir>` is where `{state}` and the project files live. Default: the current directory.",
        f"- `<workdir>/{state}` holds every resolved value. Module 00 creates it, each module reads it first, and you "
        "record what a module adds before you move on. In commands, `<key>` means the value of `key` in that file, for "
        "example `<profile>`.",
        "- A **Load:** line names a base skill, and usually one reference file in it, to read at that step. Don't load "
        "base skills up front, and read only the file named.",
        "- Scripts declare their own dependencies, so run them with `uv run <skill>/scripts/<file>.py` from any "
        "directory. Never fill `<...>` placeholders in `scripts/` by hand.",
        "",
        "## Inputs",
        "",
        "Module 00 asks for the required inputs before anything else, and doesn't continue until the user answers "
        "them. Offer the defaults for the rest.",
        "",
        "| Input | Default | Notes |",
        "|---|---|---|",
        *(f"| `{i['name']}` | — (asked) | {cell(i.get('notes') or i['ask'])} |" for i in required),
        *(f"| `{i['name']}` | {cell(i.get('default', '—'))} | {cell(i.get('notes', ''))} |" for i in optional),
        "",
        "## Guardrails",
        "",
        "- **Ask for the required inputs first** (module 00, step 1). Don't assume answers to them.",
        "- **Confirm before billable or shared actions:** "
        + ("; ".join(bp.get("billable") or []) or MARK.format("each billable or shared action"))
        + ". Show what will be created and wait for an explicit go-ahead.",
        "- **Never delete** a table, volume, job, endpoint, or app without separate confirmation that names the exact "
        "resource.",
        "- **Don't grant access** to what this skill creates unless the user names the users, groups, or service "
        "principals.",
        f"- **Never ask for or store tokens.** Use the CLI profile. `{state}` holds names and settings only.",
        "- **Don't build or suggest Agent Bricks Knowledge Assistant or Agent Bricks Information Extraction.** Both are "
        "being deprecated.",
        *(f"- {g}" for g in bp.get("guardrails") or []),
        "- **Docs beat base skills when they disagree.** Follow [Base-skill guidance to override]"
        "(#base-skill-guidance-to-override) and the doc pages the modules cite.",
        "",
        "## Workflow",
        "",
        "Run the modules in order. Each one ends with a **Verify** step that must pass before you move on. Track "
        f"progress with this checklist, and add each module's id to `completed` in `{state}` when its Verify passes:",
        "",
        f"- [ ] 00 Preflight: required inputs answered, base skills present, cost approved, `{state}` written",
        *(f"- [ ] {m['id'][:2]} {m['title']}: {m['produces']}" for m in bp["modules"][1:]),
        "",
        "| # | Module | Produces | Base skills used |",
        "|---|---|---|---|",
        *(f"| {m['id'][:2]} | [{m.get('title', 'Preflight')}](modules/{m['id']}.md) | "
          f"{cell(m.get('produces') or f'`{state}`, tooling, cost go-ahead')} | "
          f"{', '.join(d['name'] for d in m.get('base_skills', []))} |" for m in bp["modules"]),
        "",
        f"**Resuming.** If `<workdir>/{state}` exists, read it, confirm its values with the user, and rerun module 00's "
        "base-skill check (step 3). Then start at the first module missing from `completed`.",
        "",
        "## Decision points",
        "",
        "| Decision | Default | Switch when |",
        "|---|---|---|",
        *(f"| {cell(d['decision'])} | {cell(d['default'])} | {cell(d['switch_when'])} |"
          for d in bp.get("decision_points") or []),
        *([] if bp.get("decision_points")
          else [f"| {MARK.format('a choice the source makes, or a variant it supports')} | | |"]),
        "",
        "## Done when",
        "",
        f"- Module {evaluate_module(bp)['id'][:2]} meets its thresholds: "
        + ", ".join(f"`{k}` ≥ {v}" for k, v in bp["thresholds"].items()) + ". Report the run URL.",
        *(f"- {d}" for d in bp.get("done_when") or [MARK.format("what else must be true, and what to tell the user")]),
        "- You told the user what was built, where it is, and anything you skipped."
        + (" If `forge` started this skill, return to it for a next step." if routed else ""),
        "",
        "## Base-skill guidance to override",
        "",
        "Some base-skill guidance is out of date for this use case. In each of these cases, follow this skill:",
        "",
        "- **Knowledge Assistant and Information Extraction:** `databricks-agent-bricks` offers them. Both are being "
        "deprecated, so don't build or suggest them.",
        *(f"- **{o['topic']}:** {o['guidance']}" for o in bp.get("overrides") or []),
        "",
    ]
    return "\n".join(lines)


def evaluate_module(bp: dict) -> dict:
    return next(m for m in bp["modules"] if m["id"].endswith("-evaluate"))


def preflight_md(bp: dict) -> str:
    state, required = bp["state_file"], bp["inputs"]["required"]
    deps = bp["modules"][0].get("base_skills", [])
    core = next(d for d in deps if d["name"] == "databricks-core")
    others = [d for d in deps if d["name"] != "databricks-core"]
    derived = bp["inputs"].get("derived") or []
    checks = [i for i in required if i.get("check")]
    values = {"profile": "<profile>", **{i["name"]: f"<{i['name']}>" for i in required}, "completed": []}
    return "\n".join([
        "# 00 · Preflight",
        "",
        "Resolve every input, confirm the tooling, base skills, and permissions, and get a cost go-ahead before anything "
        f"billable runs. The resolved values go in `<workdir>/{state}`.",
        "",
        "## 1. Ask for the requirements",
        "",
        f"Before anything else, ask the user for these {len(required)} things in one question, and wait for answers. "
        "Don't fill them in from defaults without asking. If the user already answered one, for example in their request "
        "to `forge`, confirm that answer in the same question instead of asking it fresh:",
        "",
        "| Requirement | Records | What to ask |",
        "|---|---|---|",
        *(f"| {cell(i.get('label') or i['name'])} | `{i['name']}` | {cell(i['ask'])} |" for i in required),
        "",
        "## 2. Collect the other inputs",
        "",
        "Propose defaults for every other input in the SKILL.md Inputs table, in the same message as step 1 or right "
        "after it. Derive the following and show them back to the user, with the requirements:",
        "",
        "```",
        *(f"{d['name']:<20}= {d['from']}" for d in derived),
        *([] if derived else [MARK.format("values derived from the answers, such as names and prefixes")]),
        "```",
        "",
        "## 3. Tooling and base skills",
        "",
        load_line(core),
        "",
        "```bash",
        "databricks --version                     # >= v1.11.0",
        "uv --version",
        "databricks auth login --host <workspace-url> --profile <profile>   # only if the profile is invalid",
        "uv run <skill>/scripts/check_base_skills.py      # base skills from ucs.yaml are installed and new enough",
        "```",
        "",
        "`check_base_skills.py` compares the sibling skill folders with the versions recorded in `ucs.yaml`, and checks "
        "that every reference file the modules cite exists. If it exits non-zero, stop, and ask the user to install or "
        f"update databricks-agent-skills ({bp['upstream']} or later) in the same skills directory, for example with "
        "`databricks aitools update`.",
        "",
        "## 4. Check what the answers name",
        "",
        *(["Confirm each answer from step 1 before you continue. If a check fails, tell the user what it found and ask "
           "again; don't pick a replacement for them.", "",
           "| Requirement | Check |", "|---|---|",
           *(f"| `{i['name']}` | {cell(i['check'])} |" for i in checks), ""] if checks else []),
        *(load_line(d) for d in others),
        *([""] if others else []),
        *([] if len(checks) == len(required) else
          [MARK.format("checks for " + ", ".join(i["name"] for i in required if not i.get("check"))), ""]),
        "## 5. Permissions",
        "",
        "The user running the skill needs:",
        "",
        "| On | Privilege |",
        "|---|---|",
        *(f"| {cell(p['on'])} | {cell(p['privilege'])} |" for p in bp.get("permissions") or []),
        *([] if bp.get("permissions") else [f"| {MARK.format('each object the skill reads or creates')} | |"]),
        "",
        "Report any missing privilege. Don't try to grant it.",
        "",
        "## 6. Cost go-ahead",
        "",
        "Summarize what each step creates, then wait for an explicit yes:",
        "",
        *(f"- {b}" for b in bp.get("billable") or [MARK.format("each billable or shared action, and when it's billed")]),
        "",
        f"## 7. Write `{state}`",
        "",
        f"After the go-ahead, write the resolved values to `<workdir>/{state}`:",
        "",
        "```json",
        json.dumps(values, indent=2),
        "```",
        "",
        "## Verify",
        "",
        "- The user answered every requirement in step 1, and every other input is resolved and echoed back.",
        "- Each check in step 4 passed, or you stopped and asked again.",
        "- `check_base_skills.py` exits 0.",
        f"- The user approved the cost summary, and `{state}` exists.",
        "",
    ])


def module_md(bp: dict, module: dict) -> str:
    state = bp["state_file"]
    sources = ", ".join(f"`{s}`" for s in module.get("sources") or [])
    evaluate = module["id"].endswith("-evaluate")
    run = [
        f"Record an MLflow experiment path as `eval_experiment` in `{state}`, such as `/Users/<user>/<name>-eval`, "
        "then confirm the cost and run:", "",
        "```bash", f"uv run <skill>/scripts/evaluate.py --state <workdir>/{state}", "```", "",
        "| Metric | Threshold |", "|---|---|",
        *(f"| `{k}` | ≥ {v} |" for k, v in bp["thresholds"].items()), "",
        "The script logs the metrics to MLflow, prints the run ID, and exits with status 1 when a threshold is missed.",
        "",
    ] if evaluate else []
    return "\n".join([
        f"# {module['id'][:2]} · {module['title']}",
        "",
        module.get("purpose") or MARK.format("one sentence: what this module does and why"),
        "",
        "## Inputs → outputs",
        "",
        "- **Inputs:** " + MARK.format(f"what this module reads from `{state}` and earlier modules"),
        f"- **Outputs:** {module['produces']}",
        "",
        f"## 1. {MARK.format('the first step')}",
        "",
        *([MARK.format("move each Load line to the step that needs it"), *(load_line(d) for d in module["base_skills"]), ""]
          if module.get("base_skills") else []),
        *run,
        MARK.format("the steps" + (f", ported from {sources}" if sources else "")),
        "",
        "## Verify",
        "",
        "- " + MARK.format("what must be true before the next module runs"),
        "",
        "## Troubleshooting",
        "",
        "| Symptom | Fix |",
        "|---|---|",
        f"| {MARK.format('errors the source handles or documents, and how to fix each')} | |",
        "",
    ])


def ucs_yaml(bp: dict, skills_dir: Path) -> str:
    def dep(d: dict) -> dict:
        return {"name": d["name"], "version": installed_version(skills_dir / d["name"] / "SKILL.md"),
                "uses": d.get("uses") or []}

    modules = []
    for m in bp["modules"]:
        scripts = list(m.get("scripts") or [])
        if m["id"] == "00-preflight" and "scripts/check_base_skills.py" not in scripts:
            scripts.insert(0, "scripts/check_base_skills.py")
        if m["id"].endswith("-evaluate") and "scripts/evaluate.py" not in scripts:
            scripts.append("scripts/evaluate.py")
        entry = {"id": m["id"], "file": f"modules/{m['id']}.md", "scripts": scripts,
                 "base_skills": [dep(d) for d in m.get("base_skills") or []]}
        if m.get("external"):
            entry["external"] = m["external"]
        modules.append(entry)
    inputs = bp["inputs"]
    ucs = {
        "name": bp["name"],
        "version": "0.1.0",
        "owner": bp.get("owner") or "unassigned",
        "upstream": f"databricks/databricks-agent-skills@{bp['upstream']}",
        "upstream_min": bp["upstream"],
        "source": {**(bp.get("source") or {}), "built_with": "forge-smith", "built_on": date.today().isoformat()},
        "inputs": {"required": [i["name"] for i in inputs["required"]],
                   "optional": [i["name"] for i in inputs.get("optional") or []]},
        "shared": ["scripts/check_base_skills.py"],
        "catalog": {"title": bp["catalog"]["title"], "choose_when": bp["catalog"]["choose_when"],
                    "not_for": bp["catalog"]["not_for"], "produces": bp["catalog"]["produces"],
                    "state_file": bp["state_file"], "next": bp["catalog"].get("next") or []},
        **({"requires": bp["requires"]} if bp.get("requires") else {}),
        "modules": modules,
        "coverage_gaps": bp.get("coverage_gaps") or [],
        "facts": bp.get("facts") or [],
        "scenarios": "evals/scenarios.yaml",
    }
    header = ("# Source of truth for this use-case skill's dependency graph. Drift detection reads it, checks compare it\n"
              "# with the module text, and scripts/check_base_skills.py reads base_skills at runtime. Built by forge-smith\n"
              f"# against databricks-agent-skills {bp['upstream']}; not yet certified in a workspace.\n")
    return header + yaml.safe_dump(ucs, sort_keys=False, allow_unicode=True, width=120)


def scenarios_yaml(bp: dict) -> str:
    entry = "forge" if bp.get("routed") else bp["name"]
    inputs = "\n".join(f"      {i['name']}: null                 # smith: a value for the certification workspace"
                       for i in bp["inputs"]["required"])
    thresholds = "\n".join(f"    {k}: {v}" for k, v in bp["thresholds"].items())
    return f"""# End-to-end certification scenarios for {bp['name']}. Each run executes every module in a sandbox workspace,
# and must pass each check and the thresholds.

defaults:
  harness:
    entry: {entry}
    auto_approve_cost_gates: sandbox_only    # never auto-approve outside the certification workspace
    teardown: []                             # smith: every resource a run creates
    upstream_versions: [{bp['upstream']}, latest]
  thresholds:                                # same floors as scripts/evaluate.py THRESHOLDS
{thresholds}
  checks:
    - no_knowledge_assistant
    - no_information_extraction

scenarios:
  - id: source-example                       # smith: the source codebase's own example data, or a public dataset
    description: null
    inputs:
{inputs}
    checks: []
"""


def evaluate_py(bp: dict) -> str:
    text = (TEMPLATES / "evaluate.py").read_text()
    text = text.replace("<skill-name>", bp["name"]).replace("<state-file>", bp["state_file"])
    text = text.replace("<evaluate-module>", evaluate_module(bp)["id"][:2])
    text, count = re.subn(r"(?m)^THRESHOLDS = \{\}", f"THRESHOLDS = {bp['thresholds']!r}", text)
    if count != 1:
        raise BlueprintError("templates/evaluate.py must have one `THRESHOLDS = {}` line")
    return text


def stub(script: str, module: dict) -> str:
    sources = ", ".join(module.get("sources") or []) or "the source"
    comment = {".sql": "--", ".py": "#", ".sh": "#", ".yml": "#", ".yaml": "#"}.get(Path(script).suffix, "#")
    header = ("# /// script\n# requires-python = \">=3.11\"\n# dependencies = []\n# ///\n" if script.endswith(".py") else "")
    return f"{header}{comment} smith: {module['id']}: port {sources} here, with every setting read from the state file\n"


def scaffold(bp: dict, skills_dir: Path, out: Path) -> Path:
    if problems := check_blueprint(bp, skills_dir):
        raise BlueprintError("The blueprint isn't ready:\n- " + "\n- ".join(problems))
    folder = out / bp["name"]
    if folder.exists():
        raise BlueprintError(f"{folder} already exists. Edit it by hand, or move it aside to scaffold again.")
    files = {"SKILL.md": skill_md(bp), "ucs.yaml": ucs_yaml(bp, skills_dir), "evals/scenarios.yaml": scenarios_yaml(bp),
             "scripts/evaluate.py": evaluate_py(bp)}
    for module in bp["modules"]:
        files[f"modules/{module['id']}.md"] = preflight_md(bp) if module["id"] == "00-preflight" else module_md(bp, module)
        for script in module.get("scripts") or []:
            files.setdefault(script, stub(script, module))
    for rel, text in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text)
    shutil.copyfile(TEMPLATES / "check_base_skills.py", folder / "scripts" / "check_base_skills.py")
    return folder


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", type=Path, required=True, help="smith.json, with the blueprint under `skill`")
    p.add_argument("--skills-dir", type=Path, default=SMITH_DIR.parent, help="Folder with the installed base skills")
    p.add_argument("--out", type=Path, help="Folder to write the skill into (default: --skills-dir)")
    args = p.parse_args()
    bp = json.loads(args.state.read_text()).get("skill") or {}
    try:
        folder = scaffold(bp, args.skills_dir, args.out or args.skills_dir)
    except BlueprintError as e:
        sys.exit(str(e))
    markers = sum(path.read_text().count("smith:") for path in folder.rglob("*") if path.is_file())
    print(f"Wrote {folder} with {markers} smith: markers to fill in; then run check_skill.py.")


if __name__ == "__main__":
    main()
