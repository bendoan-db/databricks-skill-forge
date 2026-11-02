# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Check a Forge use-case skill folder against Forge's rules (forge-smith module 05). Exits 1 and lists each problem.

    uv run <smith>/scripts/check_skill.py <skill-folder> [--skills-dir <dir>]

Checks the frontmatter, the SKILL.md sections and their order, ucs.yaml against the module text (each module's
Load lines name exactly its base skills, and so does its Workflow row), module shape, inputs, thresholds, the shared
check_base_skills.py, the job tasks (each one in scripts/databricks.yml is a notebook_task whose scripts/<file>.py is a
Databricks notebook ending in the guarded run cell), leftover `smith:` markers, and the folder limits (50 entries,
3 levels, text only, no caches).
With --skills-dir, it also runs the skill's own check_base_skills.py against those base skills. The Forge repo's tests
run this on every use-case skill, so its rules stay the same as theirs.
"""

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

import yaml

SMITH_DIR = Path(__file__).resolve().parent.parent
SECTIONS = ["Reference architecture", "Conventions", "Inputs", "Guardrails", "Workflow", "Decision points", "Done when",
            "Base-skill guidance to override"]
LOAD_LINE = re.compile(r"\*\*[Ll]oad:?\*\*")
CACHES = {"__pycache__", ".ruff_cache", ".pytest_cache"}
PIN = re.compile(r"^databricks/databricks-agent-skills@v\d+\.\d+\.\d+$")
NOTEBOOK_HEADER = "# Databricks notebook source"
RUN_CELL = ("# As a job's notebook task, the settings arrive as widgets; run as a script, as command-line flags.\n"
            'if __name__ == "__main__":\n'
            '    main(notebook_argv(build_parser()) if "dbutils" in globals() else None)')
NOTEBOOK_PATH = re.compile(r"^(?:\.\./)?src/([\w.-]+\.py)$")


def frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    return (yaml.safe_load(match.group(1)) or {}) if match else {}


def thresholds(evaluate: Path) -> dict | None:
    for node in ast.parse(evaluate.read_text()).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "THRESHOLDS":
            return ast.literal_eval(node.value)
    return None


def check(folder: Path, skills_dir: Path | None = None) -> list[str]:
    problems = []
    for rel in ("SKILL.md", "ucs.yaml", "evals/scenarios.yaml", "scripts/check_base_skills.py", "scripts/evaluate.py"):
        if not (folder / rel).is_file():
            problems.append(f"{rel} is missing")
    if problems:
        return problems
    text = (folder / "SKILL.md").read_text()
    meta, ucs = frontmatter(text), yaml.safe_load((folder / "ucs.yaml").read_text()) or {}

    # Frontmatter and manifest
    if not (meta.get("name") == folder.name == ucs.get("name")):
        problems.append(f"names differ: folder {folder.name}, SKILL.md {meta.get('name')}, ucs.yaml {ucs.get('name')}")
    version = (meta.get("metadata") or {}).get("version")
    if str(version) != str(ucs.get("version")):
        problems.append(f"SKILL.md metadata.version {version} isn't ucs.yaml version {ucs.get('version')}")
    if (meta.get("metadata") or {}).get("forge_kind") != "use-case":
        problems.append("SKILL.md metadata.forge_kind must be use-case")
    if not meta.get("description") or len(meta["description"]) > 1024:
        problems.append("description must be 1 to 1024 characters")
    if len(str(meta.get("compatibility", ""))) > 500:
        problems.append("compatibility is over 500 characters")
    if not PIN.match(str(ucs.get("upstream", ""))):
        problems.append("ucs.yaml upstream must be databricks/databricks-agent-skills@vX.Y.Z")

    # SKILL.md sections, in order
    found = [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]
    if [s for s in found if s in SECTIONS] != SECTIONS:
        problems.append(f"SKILL.md sections must be, in order: {', '.join(SECTIONS)}")

    # Catalog entry and state file
    catalog = ucs.get("catalog") or {}
    for key in ("title", "choose_when", "not_for", "produces", "state_file"):
        if not catalog.get(key):
            problems.append(f"ucs.yaml catalog.{key} is missing")
    if catalog.get("state_file") and catalog["state_file"] not in text:
        problems.append(f"SKILL.md never names its state file {catalog['state_file']}")

    # Modules against the manifest
    modules = ucs.get("modules") or []
    if not modules or modules[0].get("id") != "00-preflight":
        problems.append("the first module must be 00-preflight")
    for module in modules:
        mid, path = module.get("id", "?"), folder / str(module.get("file"))
        if not path.is_file():
            problems.append(f"{mid}: {module.get('file')} is missing")
            continue
        body = path.read_text()
        if not body.startswith(f"# {mid[:2]} · "):
            problems.append(f"{module['file']}: must start with `# {mid[:2]} · <Title>`")
        if "\n## Verify" not in body:
            problems.append(f"{module['file']}: needs a ## Verify section")
        if mid != "00-preflight" and "\n## Inputs → outputs" not in body:
            problems.append(f"{module['file']}: needs a ## Inputs → outputs section")
        for script in module.get("scripts") or []:
            if not (folder / script).is_file():
                problems.append(f"{mid}: script {script} is missing")
        loads = "\n".join(line for line in body.splitlines() if LOAD_LINE.search(line))
        loaded = set(re.findall(r"`(databricks-[a-z-]+)`", loads))
        declared = {dep["name"] for dep in module.get("base_skills") or []}
        if loaded != declared:
            problems.append(f"{mid}: Load lines name {sorted(loaded)}, ucs.yaml declares {sorted(declared)}")
        if "1-knowledge-assistants.md" in loads:
            problems.append(f"{mid}: loads the Knowledge Assistant reference; Knowledge Assistant is being deprecated")
        row = next((line for line in text.splitlines() if line.startswith(f"| {mid[:2]} |")), "")
        if set(re.findall(r"databricks-[a-z-]+", row)) != declared:
            problems.append(f"SKILL.md Workflow row {mid[:2]} must list exactly {sorted(declared)}")
    for fact in ucs.get("facts") or []:
        for used in fact.get("used_in") or []:
            if not (folder / used).is_file():
                problems.append(f"fact {fact.get('fact', '?')!r}: {used} is missing")

    # Inputs: ucs.yaml, the SKILL.md table, and module 00's questions
    inputs = ucs.get("inputs") or {}
    table = section(text, "Inputs")
    for name in (inputs.get("required") or []) + (inputs.get("optional") or []):
        if f"| `{name}` |" not in table:
            problems.append(f"input {name} isn't in the SKILL.md Inputs table")
    preflight = folder / "modules" / "00-preflight.md"
    if preflight.is_file():
        asked = preflight.read_text()
        for name in inputs.get("required") or []:
            if not re.search(rf"\b{re.escape(name)}\b", asked):
                problems.append(f"required input {name} isn't asked in module 00")

    # Thresholds
    scenarios = yaml.safe_load((folder / "evals" / "scenarios.yaml").read_text()) or {}
    floors = thresholds(folder / "scripts" / "evaluate.py")
    if not floors:
        problems.append("scripts/evaluate.py needs a non-empty THRESHOLDS")
    elif floors != (scenarios.get("defaults") or {}).get("thresholds"):
        problems.append("scripts/evaluate.py THRESHOLDS and evals/scenarios.yaml defaults.thresholds differ")

    # Job and training code: notebook tasks running Databricks notebooks
    problems += job_task_problems(folder)

    # Shared file
    shared = SMITH_DIR / "templates" / "check_base_skills.py"
    if (folder / "scripts" / "check_base_skills.py").read_bytes() != shared.read_bytes():
        problems.append(f"scripts/check_base_skills.py differs from Forge's shared copy ({shared})")

    # Folder limits, text only, and leftover markers
    entries = list(folder.rglob("*"))
    if len(entries) > 50:
        problems.append(f"{len(entries)} files and folders; the limit is 50")
    if entries and max(len(p.relative_to(folder).parts) for p in entries) > 3:
        problems.append("nested more than 3 levels deep")
    for path in entries:
        rel = path.relative_to(folder).as_posix()
        if path.name in CACHES:
            problems.append(f"{rel}: remove caches from the skill folder")
        elif path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                problems.append(f"{rel}: not UTF-8 text; skill folders hold text only")
                continue
            if count := content.count("smith:"):
                problems.append(f"{rel}: {count} smith: marker(s) left to fill in")

    if skills_dir and not problems:
        result = subprocess.run([sys.executable, "-B", str(folder / "scripts" / "check_base_skills.py"),
                                 "--skills-dir", str(skills_dir)], capture_output=True, text=True, check=False)
        if result.returncode:
            problems.append("check_base_skills.py: " + (result.stderr.strip().splitlines() or ["failed"])[-1])
    return problems


def job_task_problems(folder: Path) -> list[str]:
    """Every job task in scripts/databricks.yml runs a Databricks notebook, so users can edit it in their workspace."""
    template = folder / "scripts" / "databricks.yml"
    if not template.is_file():
        return []
    try:
        bundle = yaml.safe_load(template.read_text()) or {}
    except yaml.YAMLError as e:
        return [f"scripts/databricks.yml doesn't parse: {e}"]
    problems, notebooks = [], False
    for job_key, job in ((bundle.get("resources") or {}).get("jobs") or {}).items():
        for task in (job or {}).get("tasks") or []:
            where = f"scripts/databricks.yml: job {job_key}, task {task.get('task_key')}"
            for kind in ("spark_python_task", "python_wheel_task"):
                if kind in task:
                    problems.append(f"{where} is a {kind}; run job and training code as a notebook_task")
            notebook = task.get("notebook_task")
            if not notebook:
                continue
            match = NOTEBOOK_PATH.match(str(notebook.get("notebook_path", "")))
            if not match:
                problems.append(f"{where}: notebook_path must be src/<file>.py, the bundle's copy of scripts/<file>.py")
                continue
            script = folder / "scripts" / match.group(1)
            if not script.is_file():
                problems.append(f"{where}: scripts/{match.group(1)} is missing")
                continue
            text = script.read_text()
            if not text.startswith(NOTEBOOK_HEADER):
                problems.append(f"scripts/{match.group(1)}: a job task, so it must start with `{NOTEBOOK_HEADER}`")
            if not text.rstrip().endswith(RUN_CELL):
                problems.append(f"scripts/{match.group(1)}: must end with the guarded run cell (anatomy.md, Scripts)")
            notebooks = True
    if notebooks:  # users edit the notebooks in their workspace; the skill must say how to keep an edit, and keep it
        modules = [path.read_text() for path in sorted((folder / "modules").glob("*.md"))]
        if not any("\n## Editing the job's notebooks\n" in body for body in modules):
            problems.append("job notebooks: the module that deploys the job needs an `## Editing the job's notebooks` "
                            "section (anatomy.md, Scripts)")
        render = folder / "scripts" / "render.py"
        if not render.is_file() or "def copy_edited" not in render.read_text() or "kept_edited" not in render.read_text():
            problems.append("job notebooks: scripts/render.py must keep copies the user edited (copy_edited, "
                            "kept_edited; templates/render.py)")
    return problems


def section(text: str, title: str) -> str:
    parts = text.split(f"\n## {title}\n", 1)
    return parts[1].split("\n## ", 1)[0] if len(parts) == 2 else ""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("folder", type=Path, help="The skill folder")
    p.add_argument("--skills-dir", type=Path, help="Folder with the installed base skills, to run check_base_skills.py")
    args = p.parse_args()
    problems = check(args.folder.resolve(), args.skills_dir)
    for problem in problems:
        print(f"- {problem}")
    if problems:
        sys.exit(1)
    print(f"{args.folder.name}: passes Forge's checks")


if __name__ == "__main__":
    main()
