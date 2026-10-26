"""Checks for the forge_ucs_drift job (jobs/forge_ucs_drift) that need no workspace, GitHub, or LLM."""

import io
import json
import shutil
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from conftest import SKILLS, SKILLS_DIR

ROOT = SKILLS_DIR.parent
sys.path.insert(0, str(ROOT / "jobs" / "forge_ucs_drift" / "src"))

from drift import config, context, edits, graph, impact, llm, publish, snapshot, steps  # noqa: E402
from drift.github import extract, is_extracted  # noqa: E402
from drift.store import LocalStore  # noqa: E402

# ── Fixtures: tiny upstream releases ──────────────────────────────────────────────


def upstream_tree(root: Path, skills: dict[str, dict[str, str]]) -> Path:
    """skills: {name: {path in the skill: content}}; SKILL.md frontmatter comes from a 'version' key."""
    for name, files in skills.items():
        folder = root / "skills" / name
        folder.mkdir(parents=True)
        version = files.pop("version", "0.1.0")
        description = files.pop("description", f"{name} things")
        (folder / "SKILL.md").write_text(f'---\nname: {name}\ndescription: "{description}"\nmetadata:\n  version: "{version}"\n---\n'
                                         + files.pop("SKILL.md", "body\n"))
        for path, text in files.items():
            (folder / path).parent.mkdir(parents=True, exist_ok=True)
            (folder / path).write_text(text)
    return root


def edge(module: str, base: str, cited: str = "SKILL.md") -> graph.Edge:
    return graph.Edge("forge-x", module, base, "0.1.0", cited)


# ── graph ─────────────────────────────────────────────────────────────────────────


def test_graph_follows_load_lines():
    edges = graph.edges(SKILLS_DIR)
    assert {e.forge_skill for e in edges} == {*SKILLS, "forge"}
    cited = {(e.forge_skill, e.base_skill, e.cited_file) for e in edges}
    assert ("forge-unstructured-doc-extraction", "databricks-vector-search", "references/search-modes.md") in cited
    assert ("forge-doc-qa-agent", "databricks-core", "SKILL.md") in cited  # named alone on a Load line
    # A Load line that names a file means "read only that file", so that module doesn't depend on SKILL.md:
    assert ("forge-doc-qa-agent", "databricks-apps-python", "SKILL.md") not in cited
    router = {e.base_skill for e in edges if e.module == "router"}
    assert {"databricks-ai-functions", "databricks-ml-training", "databricks-agent-bricks"} <= router


def test_every_pin_is_a_tag():
    for skill in graph.forge_skills(SKILLS_DIR).values():
        assert skill.pin.startswith("v") and skill.pin.count(".") == 2


# ── impact ────────────────────────────────────────────────────────────────────────


def test_impact_rules(tmp_path: Path):
    old = snapshot.snapshot(upstream_tree(tmp_path / "old", {
        "databricks-a": {"references/x.md": "one", "references/unread.md": "a"},
        "databricks-b": {"version": "0.1.0"},
        "databricks-c": {"references/y.md": "y"},
        "databricks-gone": {},
        "databricks-r": {"description": "old purpose"},
    }))
    new = snapshot.snapshot(upstream_tree(tmp_path / "new", {
        "databricks-a": {"references/x.md": "two", "references/unread.md": "b"},
        "databricks-b": {"version": "0.2.0"},
        "databricks-c": {},
        "databricks-r": {"description": "new purpose"},
    }))
    edges = [edge("01", "databricks-a", "references/x.md"), edge("01", "databricks-b"),
             edge("02", "databricks-c", "references/y.md"), edge("02", "databricks-gone"), edge("router", "databricks-r")]
    found = {(i.base_skill, i.kind) for i in impact.impacts(edges, old, new)}
    assert found == {("databricks-a", "cited_file_changed"), ("databricks-b", "version_changed"),
                     ("databricks-c", "cited_file_removed"), ("databricks-gone", "skill_removed"),
                     ("databricks-r", "description_changed")}
    assert impact.severity(impact.impacts(edges, old, new)) == "breaking"
    assert impact.impacts([edge("01", "databricks-a", "references/x.md")], old, old) == []


def test_skill_md_changes_count_only_where_it_is_read(tmp_path: Path):
    old = snapshot.snapshot(upstream_tree(tmp_path / "old", {"databricks-a": {"SKILL.md": "v1", "references/x.md": "x"}}))
    new = snapshot.snapshot(upstream_tree(tmp_path / "new", {"databricks-a": {"SKILL.md": "v2", "references/x.md": "x"}}))
    assert impact.impacts([edge("01", "databricks-a", "references/x.md")], old, new) == []
    assert [i.kind for i in impact.impacts([edge("01", "databricks-a")], old, new)] == ["skill_md_changed"]


def test_decide():
    def row(base: str, severity: str) -> dict:
        return {"base_skill": base, "severity": severity}

    assert steps.decide([row("a", "compatible")], []) == ("compatible", [])
    assert steps.decide([row("a", "candidate")], []) == ("none", ["a"])  # waits for its assessment
    assessed = [{"base_skill": "a", "materiality": "none", "error": None}]
    assert steps.decide([row("a", "breaking")], assessed) == ("breaking", [])  # a removed file breaks, whatever the model says
    assert steps.decide([row("a", "candidate"), row("b", "compatible")],
                        [{"base_skill": "a", "materiality": "material", "error": None}]) == ("material", [])


# ── llm ───────────────────────────────────────────────────────────────────────────


def test_prompts_fill_and_keep_braces_in_values():
    text = llm.template("assess", forge_skill="s", forge_version="1", from_tag="v1", to_tag="v2", base_skill="b",
                        context="uses {{job.start_time}} and {json}")
    assert "{{job.start_time}}" in text and "{{forge_skill}}" not in text
    with pytest.raises(KeyError):
        llm.template("assess", forge_skill="s")


def test_parse_and_check_responses():
    assert llm.parse_json('Here:\n```json\n{"materiality": "none"}\n```') == {"materiality": "none"}
    assert llm.check_assessment(llm.parse_json('{"materiality": "material", "summary": "x"}'))["affected_modules"] == []
    with pytest.raises(ValueError):
        llm.check_assessment({"materiality": "huge"})
    assert llm.check_proposal({"bump": "minor", "edits": []})["edits"] == []
    with pytest.raises(ValueError):
        llm.check_proposal({"bump": "minor"})


def test_fit_cuts_the_last_sections():
    text = llm.fit([("a", "x" * 50), ("b", "y" * 50)], budget=80)
    assert "x" * 50 in text and "y" not in text and "(cut" in text


# ── edits ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    copy = tmp_path / "repo"
    shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "dist", ".venv"))
    return copy


def test_paths_are_confined_to_the_skill():
    skill = "forge-doc-qa-agent"
    assert edits.check_path(f"skills/{skill}/modules/01-agent.md", skill) is None
    assert edits.check_path("README.md", skill) is None
    for bad in (f"skills/{skill}/../forge/SKILL.md", "/etc/passwd", "tools/build_release.py", "skills/forge/SKILL.md",
                ".github/workflows/release.yml", f"skills\\{skill}\\x"):
        assert edits.check_path(bad, skill), bad


def test_apply_edits(repo: Path):
    skill = "forge-doc-qa-agent"
    module = f"skills/{skill}/modules/01-agent.md"
    problems = edits.apply_edits(repo, skill, edits.parse_edits([
        {"path": module, "action": "replace", "old": "# 01 · Agent", "new": "# 01 · Agent (edited)"},
        {"path": module, "action": "replace", "old": "not in the file", "new": "x"},
        {"path": f"skills/{skill}/notes.md", "action": "create", "content": "new\n"},
        {"path": "tools/build_release.py", "action": "delete"},
    ]))
    assert "# 01 · Agent (edited)" in (repo / module).read_text()
    assert (repo / f"skills/{skill}/notes.md").read_text() == "new\n"
    assert (repo / "tools/build_release.py").is_file()
    assert len(problems) == 2 and "appears 0 times" in problems[0] and "outside" in problems[1]


def test_shared_files_are_copied_to_every_carrier(repo: Path):
    skill = "forge-structured-doc-extraction"
    target = repo / "skills" / skill / "scripts" / "parse.py"
    target.write_text(target.read_text() + "# changed\n")
    copied = edits.sync_shared(repo, skill, [f"skills/{skill}/scripts/parse.py"])
    assert copied == ["skills/forge-unstructured-doc-extraction/scripts/parse.py"]
    assert (repo / copied[0]).read_text() == target.read_text()


def test_version_and_pin(repo: Path):
    skill = "forge-doc-qa-agent"
    old = yaml.safe_load((repo / "skills" / skill / "ucs.yaml").read_text())["version"]
    new = edits.set_version_and_pin(repo, skill, "minor", "v9.9.9")
    ucs = yaml.safe_load((repo / "skills" / skill / "ucs.yaml").read_text())
    assert new == edits.bump(old, "minor") == ucs["version"] and ucs["upstream"] == "databricks/databricks-agent-skills@v9.9.9"
    assert f'version: "{new}"' in (repo / "skills" / skill / "SKILL.md").read_text()
    assert f"| [`{skill}`](skills/{skill}/SKILL.md) | {new} |" in (repo / "README.md").read_text()
    router = edits.set_version_and_pin(repo, "forge", "patch", "v9.9.9")  # no pin; version in forge.yaml
    assert yaml.safe_load((repo / "skills" / "forge" / "forge.yaml").read_text())["version"] == router


def test_risky_lines_are_flagged():
    diff = ("+++ b/skills/s/scripts/x.py\n+import subprocess\n+x = 1\n"
            "+++ b/skills/s/SKILL.md\n+Use subprocess wisely.\n")
    assert edits.risky_lines(diff) == ["skills/s/scripts/x.py: import subprocess"]


def test_changed_files_and_diff(repo: Path, tmp_path: Path):
    base = tmp_path / "base"
    shutil.copytree(repo, base)
    (repo / "README.md").write_text("changed\n")
    (repo / "VERSION").unlink()
    changes = edits.changed_files(base, repo)
    assert changes == {"README.md": b"changed\n", "VERSION": None}
    assert steps.diff_stat(edits.unified_diff(base, repo, list(changes)))[0].startswith("`README.md` (+1/-")


# ── publish ───────────────────────────────────────────────────────────────────────


def test_pull_request_text():
    proposal = {"run_id": "7", "forge_skill": "forge-doc-qa-agent", "action": "edit", "from_tag": "v0.2.29",
                "to_tag": "v0.2.34", "materiality": "material", "new_version": "0.4.1", "valid": False, "attempts": 2,
                "summary": "s", "failures_json": json.dumps(["test fails: t"]),
                "flags_json": json.dumps(["x.py: import subprocess"]), "artifact_dir": "/Volumes/x"}
    impacts = [{"base_skill": "databricks-apps-python", "kind": "cited_file_changed",
                "paths_json": json.dumps(["references/2-app-resources.md"]), "modules_json": json.dumps(["02-deploy-app"])}]
    assessments = [{"base_skill": "databricks-apps-python", "materiality": "material", "error": None,
                    "assessment_json": json.dumps({"summary": "Apps now grant system.ai models.", "reasons": []})}]
    plan = {"summary": "Grant the model.", "rationale": [{"path": "p", "why": "w", "evidence": [{"source": "s", "quote": "q"}]}]}
    text = publish.body(proposal, impacts, assessments, plan, "databricks/databricks-agent-skills", ["`p` (+1/-0)"])
    assert "compare/v0.2.29...v0.2.34" in text and "Failed after 2 attempt(s)" in text and "Review carefully" in text
    assert "Apps now grant system.ai models." in text and "> q" in text
    assert publish.labels(proposal) == ["drift", "drift:material", "needs-owner"]
    assert publish.title(proposal) == "drift(forge-doc-qa-agent): upstream v0.2.29 → v0.2.34"


# ── github archives ───────────────────────────────────────────────────────────────


def test_extract_strips_the_top_folder_and_refuses_escapes(tmp_path: Path):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, data in (("top/skills/a/SKILL.md", b"ok"), ("top/scripts/x.py", b"skip"), ("top/skills/../../evil", b"no")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("top/skills/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(raw.getvalue())
    extract(archive, tmp_path / "out", ("skills",))
    assert (tmp_path / "out" / "skills" / "a" / "SKILL.md").read_bytes() == b"ok"
    assert not (tmp_path / "out" / "scripts").exists() and not (tmp_path / "evil").exists()
    assert not (tmp_path / "out" / "skills" / "link").exists() and is_extracted(tmp_path / "out")


# ── the steps, end to end, with stand-ins for GitHub and the models ──────────────


def test_steps_end_to_end(tmp_path: Path, monkeypatch):
    """detect → analyze → propose → publish (dry run) on fake releases, with canned model answers."""
    # Every base skill the Forge skills load, the same in both releases, except one file doc-qa reads.
    bases = sorted({e.base_skill for e in graph.edges(SKILLS_DIR)})
    pinned = upstream_tree(tmp_path / "v1.0.0", {b: {} for b in bases} | {"databricks-apps-python": {"references/2-app-resources.md": "old"}})
    newest = upstream_tree(tmp_path / "v2.0.0", {b: {} for b in bases} | {"databricks-apps-python": {"references/2-app-resources.md": "new"}})
    forge = tmp_path / "forge"
    shutil.copytree(ROOT, forge, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "dist", ".venv"))
    ucs = forge / "skills" / "forge-doc-qa-agent" / "ucs.yaml"
    ucs.write_text(ucs.read_text().replace("databricks-agent-skills@v0.2.29", "databricks-agent-skills@v1.0.0"))
    for name in SKILLS:  # only one Forge skill is behind
        if name != "forge-doc-qa-agent":
            other = forge / "skills" / name / "ucs.yaml"
            other.write_text(other.read_text().replace("databricks-agent-skills@v0.2.34", "databricks-agent-skills@v2.0.0"))

    class FakeGitHub:
        def __init__(self, token=None):
            pass

        def branch_sha(self, repo, branch):
            return "abc123"

        def releases(self, repo):
            return [{"tag_name": "v2.0.0", "published_at": "2026-10-26T08:35:00Z", "html_url": "u2"},
                    {"tag_name": "v1.0.0", "published_at": "2026-10-20T08:35:00Z", "html_url": "u1"}]

    def fake_fetch(gh, repo, ref, dest, keep):
        source = forge if repo.endswith("skill-forge") else {"v1.0.0": pinned, "v2.0.0": newest}.get(ref)
        if source is None:  # a pin these fixtures don't have
            source = pinned
        shutil.copytree(source, dest, dirs_exist_ok=True)
        (dest / ".complete").write_text("ok")

    module = "skills/forge-doc-qa-agent/modules/02-deploy-app.md"
    original = (forge / module).read_text().splitlines()[0]
    answers = {
        "assess": json.dumps({"materiality": "material", "summary": "Apps grant model services now.",
                              "affected_modules": ["02-deploy-app"], "reasons": []}),
        "propose": json.dumps({"summary": "Note the model grant.", "bump": "patch",
                               "edits": [{"path": module, "action": "replace", "old": original, "new": original + " (edited)"}],
                               "rationale": [{"path": module, "why": "upstream", "evidence": [{"source": "s", "quote": "q"}]}]}),
    }

    def fake_ai_query(spark, model, prompts, max_tokens):
        kind = "propose" if model == "system.ai.claude-opus-5-5" else "assess"
        return [(answers[kind], None) for _ in prompts]

    monkeypatch.setattr(steps, "GitHub", FakeGitHub)
    monkeypatch.setattr(steps, "fetch", fake_fetch)
    monkeypatch.setattr(steps, "github_token", lambda settings: None)
    monkeypatch.setattr(llm, "ai_query", fake_ai_query)
    monkeypatch.setattr(steps, "get_spark", lambda: None)  # the models are stand-ins; no Spark session needed
    monkeypatch.setattr(steps.validate, "baseline", lambda repo: set())
    monkeypatch.setattr(steps.validate, "check",
                        lambda repo, skill, upstream, baseline: steps.validate.Result(True))

    settings = config.Settings(run_id="t1", local_dir=str(tmp_path / "state"))
    steps.run("all", settings)
    store = LocalStore(tmp_path / "state")
    impacts = store.rows("ucs_impacts", run_id="t1")
    assert [(i["forge_skill"], i["kind"]) for i in impacts] == [("forge-doc-qa-agent", "cited_file_changed")]
    (proposal,) = store.rows("ucs_drift_proposals", run_id="t1")
    assert (proposal["action"], proposal["valid"], proposal["new_version"]) == ("edit", True, "0.4.1")
    folder = tmp_path / "state" / "runs" / "t1" / "forge-doc-qa-agent"
    assert (folder / "files" / module).read_text().splitlines()[0].endswith("(edited)")
    assert "@v2.0.0   # moved by forge_ucs_drift" in (folder / "files" / "skills/forge-doc-qa-agent/ucs.yaml").read_text()
    assert "**Dry run.**" in (folder / "pr.md").read_text()
    assert [r["status"] for r in store.rows("drift_runs", run_id="t1")] == ["detected", "completed"]

    # Nothing moved: the next run stops at detect, until forced.
    steps.run("detect", config.Settings(run_id="t2", local_dir=str(tmp_path / "state")))
    assert store.rows("drift_runs", run_id="t2")[0]["status"] == "skipped"
    steps.run("detect", config.Settings(run_id="t3", local_dir=str(tmp_path / "state"), force=True))
    assert store.rows("drift_runs", run_id="t3")[0]["status"] == "detected"


def test_context_mentions_the_affected_module(tmp_path: Path):
    impacts = [{"kind": "cited_file_changed", "detail": "x changed", "modules_json": json.dumps(["02-deploy-app"]),
                "paths_json": json.dumps(["references/2-app-resources.md"])}]
    text = context.assess(SKILLS_DIR, "forge-doc-qa-agent", "databricks-apps-python", impacts, {"references/2-app-resources.md": "+new"}, {})
    assert "# 02 · Deploy as a Databricks App" in text and "+new" in text


def test_bundle_matches_the_steps():
    bundle = yaml.safe_load((ROOT / "jobs" / "forge_ucs_drift" / "databricks.yml").read_text())
    job = bundle["resources"]["jobs"]["forge_ucs_drift"]
    assert [t["task_key"] for t in job["tasks"]] == list(config.STEPS)
    assert {p["name"]: p["default"] for p in job["parameters"]}["dry_run"] == "${var.dry_run}"
    assert bundle["variables"]["dry_run"]["default"] == "true"  # the trial period starts in dry run
    assert bundle["variables"]["edit_model"]["default"] == config.Settings.edit_model
    assert bundle["variables"]["assess_model"]["default"] == config.Settings.assess_model
    assert bundle["targets"]["forge"]["workspace"]["profile"] == "forge"
    for task in job["tasks"]:
        args = task["spark_python_task"]["parameters"]
        assert args[0] == task["task_key"] and "{{job.run_id}}" in args
        uses_token = "--secret-key" in args
        assert uses_token == (task["task_key"] in ("detect", "publish")), "only detect and publish read the token"
