"""Checks for the forge-smith builder skill (skills/forge-smith): its scanner, scaffold, and checker. No workspace."""

import ast
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import SKILLS, SKILLS_DIR

SMITH = SKILLS_DIR / "forge-smith"
TEXT = (SMITH / "SKILL.md").read_text()
META = yaml.safe_load(TEXT.split("---")[1])


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"forge_smith__{name}", SMITH / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan, scaffold, check_skill = load("scan"), load("scaffold"), load("check_skill")


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(SMITH / "scripts" / script), *args],
                          capture_output=True, text=True, check=False)


def base_skill(skills: Path, name: str, version: str | None = "0.1.0", refs: dict[str, str] | None = None) -> None:
    folder = skills / name
    folder.mkdir(parents=True)
    meta = f'metadata:\n  version: "{version}"\n' if version else ""
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} things\n{meta}---\nbody\n")
    for ref, title in (refs or {}).items():
        (folder / ref).parent.mkdir(exist_ok=True)
        (folder / ref).write_text(f"# {title}\n\ntext\n")


@pytest.fixture
def skills(tmp_path: Path) -> Path:
    """A skills folder with a few base skills, as installed next to forge-smith."""
    out = tmp_path / "skills"
    base_skill(out, "databricks-core")
    base_skill(out, "databricks-unity-catalog", "0.3.0", {"references/1-access-control.md": "Access control"})
    base_skill(out, "databricks-spark-structured-streaming", "0.1.0",
               {"references/trigger-and-cost-optimization.md": "Triggers and cost"})
    base_skill(out, "databricks-mlflow-evaluation", "0.1.0", {"references/patterns-evaluation.md": "Evaluation patterns"})
    base_skill(out, "databricks-ai-functions", "0.2.0", {"references/1-task-functions.md": "Task functions"})
    base_skill(out, "databricks-dabs", None)
    return out


def example_blueprint() -> dict:
    """The blueprint example in references/blueprint.md."""
    text = (SMITH / "references" / "blueprint.md").read_text()
    return json.loads(re.search(r"```json\n(.*?)\n```", text, re.DOTALL).group(1))["skill"]


# ── The skill itself ─────────────────────────────────────────────────────────────


def test_frontmatter_and_layout():
    assert META["name"] == "forge-smith" and META["metadata"]["forge_kind"] == "builder"
    assert len(META["description"]) <= 1024 and len(META["compatibility"]) <= 500
    assert not (SMITH / "ucs.yaml").exists()  # a builder, not a use case: the use-case checks and the router skip it
    for link in re.findall(r"\]\(((?:modules|references)/[^)]+)\)", TEXT):
        assert (SMITH / link).is_file(), link
    modules = sorted(p.name for p in (SMITH / "modules").glob("*.md"))
    assert modules == sorted(re.findall(r"\(modules/([^)]+)\)", TEXT))
    for path in (SMITH / "modules").glob("*.md"):
        body = path.read_text()
        assert body.startswith(f"# {path.name[:2]} · ") and "\n## Verify" in body, path.name


def test_check_base_skills_template_is_the_shared_copy():
    template = (SMITH / "templates" / "check_base_skills.py").read_bytes()
    for skill in SKILLS:
        assert (SKILLS_DIR / skill / "scripts" / "check_base_skills.py").read_bytes() == template, (
            f"{skill}: copy scripts/check_base_skills.py over skills/forge-smith/templates/check_base_skills.py too")


def test_forge_points_to_smith():
    routing = yaml.safe_load((SKILLS_DIR / "forge" / "evals" / "routing.yaml").read_text())
    assert any(case["expect"].get("point_to") == "forge-smith" for case in routing["cases"])


# ── check_skill.py ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("skill", SKILLS)
def test_check_skill_passes_every_use_case_skill(skill: str):
    """The checker enforces the same rules as tests/test_skills.py: every hand-built skill passes it."""
    assert check_skill.check(SKILLS_DIR / skill) == []


def test_check_skill_catches_problems(tmp_path: Path):
    skill = "forge-unstructured-doc-extraction"
    folder = tmp_path / skill
    shutil.copytree(SKILLS_DIR / skill, folder, ignore=shutil.ignore_patterns("__pycache__"))
    module = folder / "modules" / "03-evaluate.md"
    module.write_text(module.read_text().replace("**load** `databricks-mlflow-evaluation`", "load the evaluation skill"))
    scenarios = folder / "evals" / "scenarios.yaml"
    scenarios.write_text(scenarios.read_text().replace("doc_hit_rate: 0.9", "doc_hit_rate: 0.5"))
    (folder / "modules" / "01-ingest-parse.md").write_text("# 01 · Ingest\n\n<!-- smith: steps -->\n")
    problems = "\n".join(check_skill.check(folder))
    assert "03-evaluate: Load lines name ['databricks-vector-search']" in problems
    assert "THRESHOLDS and evals/scenarios.yaml defaults.thresholds differ" in problems
    assert "01-ingest-parse.md: needs a ## Verify section" in problems and "smith: marker" in problems


# ── scan.py ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def codebase(tmp_path: Path) -> Path:
    root = tmp_path / "claims-accelerator"
    (root / "notebooks").mkdir(parents=True)
    (root / "notebooks" / "01_ingest.py").write_text(
        "# Databricks notebook source\n"
        'dbutils.widgets.text("catalog", "main")\n'
        'token = dbutils.secrets.get(scope="claims", key="api_token")\n'
        "from pyspark.sql.functions import col\n"
        'df = spark.readStream.table("main.claims.raw")\n'
        'df.writeStream.trigger(availableNow=True).toTable(f"{catalog}.claims.staged")\n'
        "parsed = spark.sql(\"SELECT ai_parse_document(content) FROM read_files('/Volumes/main/claims/docs')\")\n"
        "HOST = 'https://acme.cloud.databricks.com'\n"
        "TOKEN = 'dapi" + "0123456789abcdef" * 2 + "'\n")
    (root / "notebooks" / "02_score.sql").write_text(
        "-- Databricks notebook source\n"
        "CREATE OR REFRESH STREAMING TABLE main.claims.scored AS SELECT * FROM main.claims.staged;\n"
        "SELECT ai_query('databricks-claude-sonnet-4', note) FROM main.claims.scored;\n")
    (root / "notebooks" / "03_assistant.py").write_text("# uses a knowledge_assistant for adjusters\n")
    (root / "train.py").write_text(
        "import argparse, os\np = argparse.ArgumentParser()\np.add_argument('--threshold', default=0.7)\n"
        "MODEL = os.getenv('CLAIMS_MODEL', 'system.ai.claude-sonnet-5-5')\n"
        f"open({str(tmp_path / 'ran')!r}, 'w').write('the scan ran this file')\n")
    (root / "databricks.yml").write_text(
        "bundle:\n  name: claims\nvariables:\n  catalog:\n    default: main\n    description: Target catalog\n"
        "resources:\n  jobs:\n    claims:\n      tasks:\n"
        "        - task_key: score\n          depends_on: [{task_key: ingest}]\n"
        "          notebook_task: {notebook_path: notebooks/02_score.sql}\n"
        "        - task_key: ingest\n          notebook_task: {notebook_path: notebooks/01_ingest.py}\n"
        "          new_cluster: {node_type_id: i3.xlarge, num_workers: 2}\n"
        "      schedule: {quartz_cron_expression: '0 0 6 * * ?'}\n")
    (root / "README.md").write_text("# Claims accelerator\n## Setup\n")
    (root / "requirements.txt").write_text("databricks-sdk\n")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
    (root / "sample_data").mkdir()
    (root / "sample_data" / "claim_1.pdf").write_bytes(b"%PDF-1.7 no NUL bytes in its header")
    (root / "notebooks" / "04_save.py").write_text("# Write the results to a table\nenriched.write.mode('overwrite')\n")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "index.js").write_text("ai_query('x')")
    return root


def test_scan_inventories_the_codebase(codebase: Path, skills: Path, tmp_path: Path):
    out = tmp_path / "smith-scan.json"
    result = run("scan.py", str(codebase), "--out", str(out), "--skills-dir", str(skills))
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text())
    kinds = {f["path"]: f["kind"] for f in data["files"]}
    assert kinds["notebooks/01_ingest.py"] == "notebook" and kinds["notebooks/02_score.sql"] == "notebook"
    assert kinds["databricks.yml"] == "bundle" and "node_modules/x/index.js" not in kinds
    assert data["skipped"]["binary_or_over_1mb"] == 2 and data["skipped"]["vendored_or_cache_dirs"] == 1
    assert [f["path"] for f in data["data_files"]] == ["logo.png", "sample_data/claim_1.pdf"]  # listed, never read
    assert data["numbered_files"] == ["notebooks/01_ingest.py", "notebooks/02_score.sql", "notebooks/03_assistant.py",
                                      "notebooks/04_save.py"]

    signals = {s["base_skill"]: s for s in data["signals"]}
    assert {"databricks-ai-functions", "databricks-spark-structured-streaming", "databricks-pipelines", "databricks-dabs",
            "databricks-serverless-migration", "databricks-unity-gateway"} <= set(signals)
    assert signals["databricks-ai-functions"]["installed"] and not signals["databricks-pipelines"]["installed"]
    assert [d["product"] for d in data["deprecated"]] == ["Agent Bricks Knowledge Assistant"]

    settings = {(s["kind"], s["name"]): s["default"] for s in data["settings"]}
    assert settings[("widget", "catalog")] == "main" and settings[("argument", "--threshold")] == "0.7"
    assert settings[("environment", "CLAIMS_MODEL")] == "system.ai.claude-sonnet-5-5"
    assert data["secrets"][0]["scope"] == "claims" and data["secrets"][0]["key"] == "api_token"

    resources = {(r["kind"], r["value"]) for r in data["resources"]}
    assert {("table", "main.claims.raw"), ("table", "main.claims.scored"), ("table", "{catalog}.claims.staged"),
            ("volume_path", "/Volumes/main/claims/docs"), ("model_or_endpoint", "databricks-claude-sonnet-4"),
            ("cluster", "i3.xlarge"), ("schedule", "0 0 6 * * ?"),
            ("workspace_host", "https://acme.cloud.databricks.com")} <= resources
    assert not any(value.startswith("pyspark.") for _, value in resources)  # an import isn't a table
    assert not any(value.startswith("enriched.") for _, value in resources)  # nor is a method call after "table"
    assert not any(value == "databricks-sdk" for _, value in resources)    # a package isn't an endpoint
    assert data["hard_coded_tokens"] == ["notebooks/01_ingest.py:9"] and "dapi" not in out.read_text()

    (job,) = data["bundles"][0]["jobs"].values()
    assert [t["task_key"] for t in job["tasks"]] == ["ingest", "score"]  # dependency order, not file order
    assert job["trigger"] == ["schedule"] and data["bundles"][0]["variables"]["catalog"]["default"] == "main"
    assert data["base_skills"]["databricks-unity-catalog"]["references"] == {"references/1-access-control.md": "Access control"}
    assert data["base_skills"]["databricks-dabs"]["version"] == "unversioned"


def test_scan_never_runs_the_code(codebase: Path, skills: Path, tmp_path: Path):
    run("scan.py", str(codebase), "--out", str(tmp_path / "scan.json"), "--skills-dir", str(skills))
    assert not (tmp_path / "ran").exists()


def test_scan_lists_installed_forge_use_cases(tmp_path: Path, codebase: Path):
    out = tmp_path / "scan.json"
    assert run("scan.py", str(codebase), "--out", str(out), "--skills-dir", str(SKILLS_DIR)).returncode == 0
    use_cases = json.loads(out.read_text())["forge_use_cases"]
    assert set(use_cases) == set(SKILLS) and use_cases["forge-doc-qa-agent"]["state_file"] == "doc-qa.json"


# ── scaffold.py, then check_skill.py ─────────────────────────────────────────────


def fill(folder: Path) -> None:
    """Stand in for the agent: replace every smith: marker."""
    for path in folder.rglob("*"):
        if path.is_file():
            text = re.sub(r"<!-- smith: [^>]*-->", "Filled in.", path.read_text())
            text = re.sub(r"(#|--) smith: [^\n]*", r"\1 filled in", text)
            path.write_text(text)


def test_scaffold_writes_a_skill_that_passes_once_filled(skills: Path, tmp_path: Path):
    state = tmp_path / "smith.json"
    state.write_text(json.dumps({"skill": example_blueprint()}))
    result = run("scaffold.py", "--state", str(state), "--skills-dir", str(skills))
    assert result.returncode == 0, result.stderr
    folder = skills / "forge-claims-triage"
    problems = check_skill.check(folder)
    assert problems and all("smith: marker" in p for p in problems), problems  # only what the agent writes is missing

    ucs = yaml.safe_load((folder / "ucs.yaml").read_text())
    assert ucs["upstream"] == "databricks/databricks-agent-skills@v0.2.35" and ucs["inputs"]["required"] == [
        "claims_table", "continuous"]
    assert {d["name"]: d["version"] for d in ucs["modules"][0]["base_skills"]} == {
        "databricks-core": "0.1.0", "databricks-unity-catalog": "0.3.0"}  # read from the installed skills
    assert ucs["modules"][-1]["scripts"] == ["scripts/evaluate.py"] and ucs["catalog"]["state_file"] == "claims.json"
    preflight = (folder / "modules" / "00-preflight.md").read_text()
    assert "| Where new claims land | `claims_table` | The three-part name" in preflight
    assert "`databricks tables get <claims_table> --profile <profile>` succeeds" in preflight
    assert "**Load:** `databricks-unity-catalog` `references/1-access-control.md` (effective grants)." in preflight
    shared = (SMITH / "templates" / "check_base_skills.py").read_bytes()
    assert (folder / "scripts" / "check_base_skills.py").read_bytes() == shared
    assert "THRESHOLDS = {'precision_at_review': 0.8}" in (folder / "scripts" / "evaluate.py").read_text()
    ingest = (folder / "scripts" / "ingest.py").read_text()  # job code: a Databricks notebook stub
    assert ingest.startswith(check_skill.NOTEBOOK_HEADER) and ingest.rstrip().endswith(check_skill.RUN_CELL)
    assert "# smith: 01-ingest: port notebooks/01_ingest.py" in ingest

    fill(folder)
    assert check_skill.check(folder, skills) == []  # including its own check_base_skills.py against the skills
    assert run("check_skill.py", str(folder), "--skills-dir", str(skills)).returncode == 0

    again = run("scaffold.py", "--state", str(state), "--skills-dir", str(skills))
    assert again.returncode == 1 and "already exists" in again.stderr


def job_blueprint() -> dict:
    """The example with a job template and an app module: job code is a notebook, app code a module."""
    bp = example_blueprint()
    bp["modules"][1]["scripts"] = ["scripts/render.py", "scripts/ingest.py", "scripts/score.py", "scripts/databricks.yml"]
    bp["modules"][1]["app_scripts"] = ["scripts/app/app.py"]
    return bp


def test_scaffold_runs_job_code_as_notebook_tasks(skills: Path, tmp_path: Path):
    folder = scaffold.scaffold(job_blueprint(), skills, tmp_path / "out")
    tasks = yaml.safe_load((folder / "scripts" / "databricks.yml").read_text())["resources"]["jobs"]["job"]["tasks"]
    assert [(t["task_key"], t["notebook_task"]["notebook_path"]) for t in tasks] == [
        ("ingest", "src/ingest.py"), ("score", "src/score.py")]
    assert tasks[1]["depends_on"] == [{"task_key": "ingest"}] and tasks[0]["notebook_task"]["base_parameters"] == {}
    assert (folder / "scripts" / "score.py").read_text().startswith(check_skill.NOTEBOOK_HEADER)
    app = (folder / "scripts" / "app" / "app.py").read_text()
    assert "Databricks notebook" not in app and "smith: 01-ingest" in app  # app code stays a module
    assert "scripts/app/app.py" in yaml.safe_load((folder / "ucs.yaml").read_text())["modules"][1]["scripts"]
    fill(folder)
    assert check_skill.check(folder, skills) == []
    for script in ("ingest.py", "score.py"):  # a filled notebook stub is still Python
        ast.parse((folder / "scripts" / script).read_text())


def test_check_skill_requires_notebook_job_tasks(skills: Path, tmp_path: Path):
    folder = scaffold.scaffold(job_blueprint(), skills, tmp_path / "out")
    fill(folder)
    template = folder / "scripts" / "databricks.yml"
    template.write_text(template.read_text().replace(
        "notebook_task:\n            notebook_path: src/score.py",
        "spark_python_task:\n            python_file: src/score.py"))
    ingest = folder / "scripts" / "ingest.py"
    ingest.write_text(ingest.read_text().removeprefix(check_skill.NOTEBOOK_HEADER + "\n"))
    problems = "\n".join(check_skill.check(folder))
    assert "task score is a spark_python_task" in problems
    assert "scripts/ingest.py: a job task, so it must start with `# Databricks notebook source`" in problems


def test_check_skill_requires_a_way_to_keep_workspace_edits(skills: Path, tmp_path: Path):
    folder = scaffold.scaffold(job_blueprint(), skills, tmp_path / "out")
    fill(folder)
    module = folder / "modules" / "01-ingest.md"
    module.write_text(module.read_text().replace("## Editing the job's notebooks", "## Notebooks"))
    (folder / "scripts" / "render.py").write_text("# renders the bundle\n")
    problems = "\n".join(check_skill.check(folder))
    assert "needs an `## Editing the job's notebooks` section" in problems
    assert "scripts/render.py must keep copies the user edited" in problems


def test_generated_render_keeps_workspace_edits(skills: Path, tmp_path: Path):
    """A generated skill's render.py copies its notebooks with a checksum line, and keeps a copy the user changed."""
    folder = scaffold.scaffold(example_blueprint(), skills, tmp_path / "out")
    fill(folder)
    assert "## Editing the job's notebooks" in (folder / "modules" / "01-ingest.md").read_text()
    state = tmp_path / "work" / "claims.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"bundle_name": "claims-job", "profile": "DEFAULT"}))
    render = [sys.executable, "-B", str(folder / "scripts" / "render.py"), "--state", str(state)]
    first = subprocess.run(render, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    bundle = state.parent / "claims-job"
    assert yaml.safe_load((bundle / "databricks.yml").read_text())["bundle"]["name"] == "claims-job"
    copy = bundle / "src" / "ingest.py"
    lines = copy.read_text().splitlines()
    assert lines[0] == check_skill.NOTEBOOK_HEADER and lines[1].startswith("# Copied from forge-claims-triage")

    copy.write_text(copy.read_text().replace("# filled in", "# changed in the workspace", 1))
    again = subprocess.run(render, capture_output=True, text=True, check=False)
    assert json.loads(again.stdout)["kept_edited"] == [str(copy)] and "changed in the workspace" in copy.read_text()
    forced = subprocess.run(render + ["--force"], capture_output=True, text=True, check=False)
    assert str(copy) in json.loads(forced.stdout)["written"] and "changed in the workspace" not in copy.read_text()


def test_render_template_copies_like_the_pipeline_skills():
    """templates/render.py's copy helpers are the hand-built pipeline skills', so every Forge skill keeps edits the same way."""
    def parts(path: Path) -> dict[str, str]:
        text = path.read_text()
        return {getattr(n, "name", None) or n.targets[0].id: ast.get_source_segment(text, n) for n in ast.parse(text).body
                if (isinstance(n, ast.FunctionDef) and n.name in ("digest", "mark_copy", "copy_edited"))
                or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in ("COPY_MARK", "COPY_MARK_RE"))}

    template = parts(SMITH / "templates" / "render.py")
    assert len(template) == 5
    for skill in ("forge-structured-doc-extraction", "forge-unstructured-doc-extraction", "forge-evaluate-guardrails"):
        assert parts(SKILLS_DIR / skill / "scripts" / "render.py") == template, f"{skill}/scripts/render.py"


def test_notebook_template_helpers_match_the_shared_parse_stage():
    """templates/notebook.py carries the reference notebook's helpers; the shared parse.py is that reference."""
    def functions(path: Path) -> dict[str, str]:
        text = path.read_text()
        return {n.name: ast.get_source_segment(text, n) for n in ast.parse(text).body if isinstance(n, ast.FunctionDef)}

    template = functions(SMITH / "templates" / "notebook.py")
    parse = functions(SKILLS_DIR / "forge-structured-doc-extraction" / "scripts" / "parse.py")
    for name in ("notebook_argv", "script_dir", "get_spark"):
        assert template[name] == parse[name], f"templates/notebook.py {name}() differs from parse.py's"
    run_cell = (SMITH / "templates" / "notebook.py").read_text().rstrip()
    assert run_cell.endswith(check_skill.RUN_CELL)


def test_routed_skills_start_through_forge(skills: Path, tmp_path: Path):
    bp = example_blueprint() | {"routed": False}
    folder = scaffold.scaffold(bp, skills, tmp_path / "out")
    assert "Run it by name" in (folder / "SKILL.md").read_text()
    assert yaml.safe_load((folder / "evals" / "scenarios.yaml").read_text())["defaults"]["harness"]["entry"] == bp["name"]
    routed = scaffold.scaffold(example_blueprint() | {"name": "forge-claims-routed"}, skills, tmp_path / "out")
    assert "`forge` skill (`<skill>/../forge`) usually starts it" in (routed / "SKILL.md").read_text()


def test_blueprint_example_is_valid(skills: Path):
    assert scaffold.check_blueprint(example_blueprint(), skills) == []


@pytest.mark.parametrize("change, expected", [
    (lambda bp: bp["modules"][1]["base_skills"].append({"name": "databricks-lakebase"}), "databricks-lakebase isn't installed"),
    (lambda bp: bp["modules"][1]["base_skills"][0].update(reference="references/missing.md"), "has no references/missing.md"),
    (lambda bp: bp["modules"].pop(), "must be an evaluate module"),
    (lambda bp: bp["modules"][0]["base_skills"].pop(0), "must load databricks-core"),
    (lambda bp: bp["modules"].reverse(), "first module must be 00-preflight"),
    (lambda bp: bp["inputs"]["required"][0].pop("ask"), "required inputs need `ask`"),
    (lambda bp: bp.update(state_file="Claims.JSON"), "state_file"),
    (lambda bp: bp.update(upstream="latest"), "upstream"),
    (lambda bp: bp["modules"][1].update(scripts=["scripts/ingest.py"]), "job notebooks need scripts/databricks.yml"),
    (lambda bp: bp["modules"][1].update(app_scripts=["app/app.py"]), "app script"),
])
def test_scaffold_refuses_unready_blueprints(skills: Path, change, expected: str):
    bp = example_blueprint()
    change(bp)
    assert any(expected in p for p in scaffold.check_blueprint(bp, skills)), scaffold.check_blueprint(bp, skills)
