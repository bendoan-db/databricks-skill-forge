"""The job's four tasks. Each reads what the earlier ones wrote (tables, and files under the volume), so any task
can be retried alone.

Only detect and publish read the GitHub token. analyze and propose never do, because propose runs code the
model wrote when it checks a proposal.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from . import context, edits, graph, impact, llm, publish, snapshot, validate
from .config import Settings
from .github import GitHub, extract, is_extracted
from .store import DeltaStore, LocalStore, now

ASSESS_TOKENS = 4096
EDIT_TOKENS = 32000
EDIT_ATTEMPTS = 2
ROUTER = "forge"
COMMIT_PREFIX = "forge_ucs_drift"
MATERIAL_RANK = {"none": 0, "compatible": 1, "material": 2, "breaking": 3}


def get_spark():
    if "DATABRICKS_RUNTIME_VERSION" in os.environ:
        from pyspark.sql import SparkSession

        return SparkSession.builder.getOrCreate()
    from databricks.connect import DatabricksSession

    return DatabricksSession.builder.serverless().getOrCreate()


def github_token(settings: Settings) -> str | None:
    if os.environ.get("FORGE_GIT_TOKEN"):
        return os.environ["FORGE_GIT_TOKEN"]
    if settings.local_dir:
        return None  # local development reads the public repos without a token
    from databricks.sdk.runtime import dbutils

    return dbutils.secrets.get(settings.secret_scope, settings.secret_key)


def version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag.lstrip("v").split("."))


class Context:
    def __init__(self, settings: Settings):
        self.s = settings
        self.root = settings.root
        self._spark = None
        self._store = None

    @property
    def spark(self):
        if self._spark is None:
            self._spark = get_spark()
        return self._spark

    @property
    def store(self):
        if self._store is None:
            self._store = LocalStore(self.root) if self.s.local_dir else DeltaStore(self.spark, self.s)
        return self._store

    def upstream_dir(self, tag: str) -> Path:
        return self.root / "upstream" / tag

    def forge_dir(self, sha: str) -> Path:
        return self.root / "forge" / sha

    def skill_dir(self, skill: str) -> Path:
        return self.root / "runs" / self.s.run_id / skill

    def run_record(self) -> dict:
        rows = [r for r in self.store.rows("drift_runs", run_id=self.s.run_id) if r["status"] in ("detected", "skipped")]
        if not rows:
            raise SystemExit(f"Run {self.s.run_id} has no detect record: run the detect task first.")
        return max(rows, key=lambda r: str(r["at"]))  # a repaired run can have several

    def wanted(self, skill: str) -> bool:
        return not self.s.skills or skill in self.s.skills


def fetch(gh: GitHub, repo: str, ref: str, dest: Path, keep: tuple[str, ...]) -> None:
    folder = Path(tempfile.mkdtemp(prefix="forge-drift-download-"))
    try:
        gh.download_tarball(repo, ref, folder / "archive.tar.gz")
        extract(folder / "archive.tar.gz", dest, keep)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def pins(skills: dict[str, graph.ForgeSkill], skills_dir: Path) -> dict[str, str]:
    """The release each Forge skill is compared from: its pin. The router has none, so it's compared from the
    newest release any use-case skill was written against."""
    found = {name: skill.pin for name, skill in skills.items()}
    if (skills_dir / ROUTER / "SKILL.md").is_file() and found:
        found[ROUTER] = max(found.values(), key=version_key)
    return found


def skill_version(skills_dir: Path, name: str) -> str:
    folder = skills_dir / name
    manifest = folder / ("ucs.yaml" if (folder / "ucs.yaml").is_file() else "forge.yaml")
    return str(yaml.safe_load(manifest.read_text())["version"])


# ── 1. detect: new releases, and the trees the later tasks compare ────────────────
def detect(ctx: Context) -> None:
    s, store = ctx.s, ctx.store
    gh = GitHub(github_token(s))  # reads only; a token avoids the low anonymous rate limit
    forge_sha = gh.branch_sha(s.forge_repo, s.forge_branch)
    forge = ctx.forge_dir(forge_sha)
    if not is_extracted(forge):
        fetch(gh, s.forge_repo, forge_sha, forge, ())
    releases = sorted(gh.releases(s.upstream_repo), key=lambda r: r["published_at"], reverse=True)
    if not releases:
        raise SystemExit(f"{s.upstream_repo} has no published releases")
    latest = releases[0]["tag_name"]
    known = {r["tag"] for r in store.rows("upstream_releases")}
    store.append("upstream_releases", [
        {"tag": r["tag_name"], "published_at": r["published_at"], "html_url": r["html_url"], "first_seen_run": s.run_id}
        for r in releases if r["tag_name"] not in known])

    skills = graph.forge_skills(forge / "skills")
    snapped = {r["tag"] for r in store.rows("upstream_skills")}
    for tag in sorted({latest, *(k.pin for k in skills.values())}, key=version_key):
        tree = ctx.upstream_dir(tag)
        if not is_extracted(tree):
            fetch(gh, s.upstream_repo, tag, tree, snapshot.ROOTS)
        if tag not in snapped:
            store.append("upstream_skills", [
                {"tag": tag, "skill": name, "dir": info["dir"], "experimental": info["experimental"],
                 "version": info["version"], "description": info["description"], "files_json": json.dumps(info["files"])}
                for name, info in snapshot.snapshot(tree).items()])

    # Nothing to do when neither upstream nor Forge moved since the last clean run in the same mode.
    done = [r for r in store.rows("drift_runs") if r["status"] == "completed" and bool(r["dry_run"]) == s.dry_run]
    last = max(done, key=lambda r: str(r["at"]), default=None)
    unchanged = bool(last) and last["latest_tag"] == latest and last["forge_sha"] == forge_sha
    status = "skipped" if unchanged and not s.force else "detected"
    message = (f"Nothing new since run {last['run_id']}." if status == "skipped"
               else f"Upstream {latest}; Forge {s.forge_branch} at {forge_sha[:12]}.")
    store.append("drift_runs", [{"run_id": s.run_id, "at": now(), "status": status, "dry_run": s.dry_run,
                                 "forge_sha": forge_sha, "latest_tag": latest, "message": message}])
    print(message)


# ── 2. analyze: what changed under each Forge skill, and does it matter? ──────────
def analyze(ctx: Context) -> None:
    s, store = ctx.s, ctx.store
    run = ctx.run_record()
    if run["status"] == "skipped":
        print("Skipped: nothing new.")
        return
    skills_dir = ctx.forge_dir(run["forge_sha"]) / "skills"
    latest = run["latest_tag"]
    skills = graph.forge_skills(skills_dir)
    all_edges = graph.edges(skills_dir)
    targets = {k: v for k, v in pins(skills, skills_dir).items() if ctx.wanted(k)}
    for table in ("ucs_dependencies", "ucs_impacts", "ucs_drift_assessments"):  # a retried task starts clean
        store.delete(table, s.run_id)
    store.append("ucs_dependencies", [
        {"run_id": s.run_id, "forge_skill": e.forge_skill, "module": e.module, "base_skill": e.base_skill,
         "base_version": e.base_version, "cited_file": e.cited_file} for e in all_edges if e.forge_skill in targets])

    snaps: dict[str, dict] = {}

    def snap(tag: str) -> dict:
        if tag not in snaps:
            snaps[tag] = snapshot.snapshot(ctx.upstream_dir(tag))
        return snaps[tag]

    impact_rows, pending = [], []
    for skill, from_tag in sorted(targets.items()):
        if from_tag == latest:
            continue
        old, new = snap(from_tag), snap(latest)
        found = impact.impacts([e for e in all_edges if e.forge_skill == skill], old, new)
        rows = [{"run_id": s.run_id, "forge_skill": skill, "from_tag": from_tag, "to_tag": latest,
                 "base_skill": i.base_skill, "kind": i.kind, "severity": i.severity, "paths_json": json.dumps(i.paths),
                 "modules_json": json.dumps(i.modules), "detail": i.detail} for i in found]
        impact_rows += rows
        for base in sorted({r["base_skill"] for r in rows}):
            base_rows = [r for r in rows if r["base_skill"] == base]
            diffs = {}
            if base in old and base in new:
                for path in sorted({p for r in base_rows for p in json.loads(r["paths_json"])}):
                    diffs[path] = impact.file_diff(ctx.upstream_dir(from_tag) / old[base]["dir"],
                                                   ctx.upstream_dir(latest) / new[base]["dir"], path, from_tag, latest)
                    out = ctx.skill_dir(skill) / "diffs" / f"{base}__{path.replace('/', '__')}.diff"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(diffs[path])
            if any(r["severity"] in ("candidate", "breaking") for r in base_rows):
                pending.append((skill, base, from_tag, base_rows, diffs))
    store.append("ucs_impacts", impact_rows)
    print(f"{len(impact_rows)} impacts on {len({r['forge_skill'] for r in impact_rows})} Forge skills; "
          f"{len(pending)} to assess.")

    prompts = [llm.template("assess", forge_skill=skill, forge_version=skill_version(skills_dir, skill), from_tag=from_tag,
                            to_tag=latest, base_skill=base,
                            context=context.assess(skills_dir, skill, base, rows, diffs, snap(latest)))
               for skill, base, from_tag, rows, diffs in pending]
    if s.no_llm:
        results = [(None, "not assessed (--no-llm)")] * len(prompts)
    else:
        results = llm.ai_query(ctx.spark, s.assess_model, prompts, ASSESS_TOKENS)
    assessments = []
    for (skill, base, from_tag, _, _), (response, error) in zip(pending, results):
        materiality, summary, detail = None, "", "{}"
        if not error:
            try:
                value = llm.check_assessment(llm.parse_json(response))
                materiality, summary, detail = value["materiality"], value["summary"], json.dumps(value)
            except ValueError as e:  # json.JSONDecodeError is a ValueError
                error = f"unreadable response: {e}"
        out = ctx.skill_dir(skill) / "assessments" / f"{base}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"response": response, "error": error}, indent=1))
        assessments.append({"run_id": s.run_id, "forge_skill": skill, "base_skill": base, "from_tag": from_tag,
                            "to_tag": latest, "materiality": materiality, "summary": summary, "assessment_json": detail,
                            "error": error, "model": s.assess_model, "prompt_version": llm.PROMPT_VERSION})
    store.append("ucs_drift_assessments", assessments)


def decide(skill_impacts: list[dict], skill_assessments: list[dict]) -> tuple[str, list[str]]:
    """A Forge skill's materiality, and the base skills still waiting for an assessment. A removed skill or cited
    file is breaking whatever the model says; a version-only change is compatible without asking it."""
    assessed = {a["base_skill"]: a for a in skill_assessments}
    level, missing = "none", []
    for base in sorted({i["base_skill"] for i in skill_impacts}):
        rows = [i for i in skill_impacts if i["base_skill"] == base]
        if all(r["severity"] == "compatible" for r in rows):
            found = "compatible"
        else:
            a = assessed.get(base)
            if not a or a.get("error") or not a.get("materiality"):
                missing.append(base)
                continue
            found = "breaking" if any(r["severity"] == "breaking" for r in rows) else a["materiality"]
        level = max(level, found, key=MATERIAL_RANK.get)
    return level, missing


# ── 3. propose: edit (or just re-pin) each Forge skill, and check the result ──────
def edit_plan(ctx: Context, repo: Path, skill: str, impacts: list[dict], assessments: list[dict],
              latest: str, feedback: list[str]) -> tuple[dict, list[str]]:
    """Ask the edit model for a proposal and apply its edits to repo. Returns (plan, problems)."""
    diffs, new_files = {}, {}
    folder = ctx.skill_dir(skill) / "diffs"
    newest = snapshot.snapshot(ctx.upstream_dir(latest))
    for i in impacts:
        for path in json.loads(i["paths_json"]):
            name = f"{i['base_skill']}/{path}"
            diff_file = folder / f"{i['base_skill']}__{path.replace('/', '__')}.diff"
            diffs[name] = diff_file.read_text() if diff_file.is_file() else ""
            info = newest.get(i["base_skill"])
            if info:
                new_files[f"{name}@{latest}"] = context.read(ctx.upstream_dir(latest) / info["dir"] / path)
    note, ctx_text = context.propose(repo, skill, impacts, assessments, diffs, new_files, feedback)
    prompt = llm.template("propose", forge_skill=skill, forge_version=skill_version(repo / "skills", skill),
                          from_tag=impacts[0]["from_tag"], to_tag=latest, feedback=note, context=ctx_text)
    (response, error), = llm.ai_query(ctx.spark, ctx.s.edit_model, [prompt], EDIT_TOKENS)
    if error:
        return {}, [f"the edit model failed: {error[:300]}"]
    try:
        plan = llm.check_proposal(llm.parse_json(response))
        problems = edits.apply_edits(repo, skill, edits.parse_edits(plan["edits"]))
    except ValueError as e:
        return {}, [f"unreadable proposal: {e}"]
    return plan, problems


def propose(ctx: Context) -> None:
    s, store = ctx.s, ctx.store
    run = ctx.run_record()
    if run["status"] == "skipped":
        print("Skipped: nothing new.")
        return
    latest = run["latest_tag"]
    impacts = store.rows("ucs_impacts", run_id=s.run_id)
    assessments = store.rows("ucs_drift_assessments", run_id=s.run_id)
    if not impacts:
        print("No Forge skill is affected.")
        return
    work = Path(tempfile.mkdtemp(prefix="forge-drift-propose-"))
    base = work / "main"
    shutil.copytree(ctx.forge_dir(run["forge_sha"]), base, ignore=shutil.ignore_patterns(".complete"))
    upstream_skills = ctx.upstream_dir(latest) / "skills"
    baseline: set[str] | None = None
    rows = []
    for skill in sorted({i["forge_skill"] for i in impacts}):
        skill_impacts = [i for i in impacts if i["forge_skill"] == skill]
        skill_assessments = [a for a in assessments if a["forge_skill"] == skill]
        materiality, missing = decide(skill_impacts, skill_assessments)
        record = {"run_id": s.run_id, "forge_skill": skill, "from_tag": skill_impacts[0]["from_tag"], "to_tag": latest,
                  "materiality": materiality, "new_version": None, "valid": False, "attempts": 0, "summary": "",
                  "failures_json": "[]", "flags_json": "[]", "artifact_dir": str(ctx.skill_dir(skill)),
                  "model": "", "prompt_version": llm.PROMPT_VERSION}
        if missing:
            rows.append({**record, "action": "unassessed", "summary": f"Not assessed yet: {', '.join(missing)}"})
            continue
        if materiality == "none" or (skill == ROUTER and materiality == "compatible"):  # the router has no pin to move
            rows.append({**record, "action": "none", "valid": True, "summary": "Nothing the skill relies on changed."})
            continue
        if baseline is None:
            baseline = validate.baseline(base)
        action = "pin_bump" if materiality == "compatible" else "edit"
        plan, result, attempts, feedback, candidate = {}, None, 0, [], base
        while attempts < (EDIT_ATTEMPTS if action == "edit" else 1):
            attempts += 1
            candidate = work / f"{skill}-{attempts}"
            shutil.copytree(base, candidate)
            problems = []
            if action == "edit":
                plan, problems = edit_plan(ctx, candidate, skill, skill_impacts, skill_assessments, latest, feedback)
            if not problems:
                changed = list(edits.changed_files(base, candidate))
                edits.sync_shared(candidate, skill, changed)
                level = plan.get("bump", "patch") if plan.get("edits") else "patch"
                record["new_version"] = edits.set_version_and_pin(candidate, skill, level, latest)
                regenerate = subprocess.run([sys.executable, "-B", "tools/build_forge_catalog.py"], cwd=candidate,
                                            env=validate.clean_env(work), capture_output=True, text=True)
                if regenerate.returncode != 0:
                    problems.append(f"tools/build_forge_catalog.py: {regenerate.stderr.strip()[-300:]}")
            result = (validate.check(candidate, skill, upstream_skills, baseline) if not problems
                      else validate.Result(False, problems))
            if result.passed:
                break
            feedback = result.failures + ([result.log] if result.log else [])
        if action == "edit" and plan and not plan.get("edits"):
            action = "pin_bump"  # the edit model found nothing to change beyond the pin
        changes = edits.changed_files(base, candidate)
        diff = edits.unified_diff(base, candidate, list(changes))
        save_artifacts(ctx.skill_dir(skill), plan, changes, diff, result)
        rows.append({**record, "action": action, "valid": result.passed, "attempts": attempts,
                     "summary": plan.get("summary") or f"Moves the upstream pin to {latest}; nothing else needed changing.",
                     "failures_json": json.dumps(result.failures), "flags_json": json.dumps(edits.risky_lines(diff)),
                     "model": s.edit_model if action == "edit" else ""})
    store.delete("ucs_drift_proposals", s.run_id)
    store.append("ucs_drift_proposals", rows)
    shutil.rmtree(work, ignore_errors=True)
    for r in rows:
        print(f"{r['forge_skill']}: {r['action']} ({r['materiality']}), valid={r['valid']}")


def save_artifacts(folder: Path, plan: dict, changes: dict[str, bytes | None], diff: str, result) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(folder / "files", ignore_errors=True)
    for path, content in changes.items():
        if content is not None:
            target = folder / "files" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    (folder / "deleted.json").write_text(json.dumps([p for p, c in changes.items() if c is None]))
    (folder / "plan.json").write_text(json.dumps(plan, indent=1))
    (folder / "changes.diff").write_text(diff)
    (folder / "validation.txt").write_text("\n".join(result.failures) + ("\n\n" + result.log if result.log else ""))


def diff_stat(diff: str) -> list[str]:
    """`path` (+added/-removed) for each file in a unified diff."""
    counts: dict[str, list[int]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            current = line[len("+++ b/"):] if line.startswith("+++ b/") else line[4:]
            counts.setdefault(current, [0, 0])
        elif current and line.startswith("+"):
            counts[current][0] += 1
        elif current and line.startswith("-"):
            counts[current][1] += 1
    return [f"`{path}` (+{added}/-{removed})" for path, (added, removed) in counts.items()]


# ── 4. publish: one pull request per Forge skill (or, in a dry run, its text) ─────
def publish_step(ctx: Context) -> None:
    s, store = ctx.s, ctx.store
    run = ctx.run_record()
    if run["status"] == "skipped":
        print("Skipped: nothing new.")
        return
    proposals = store.rows("ucs_drift_proposals", run_id=s.run_id)
    impacts = store.rows("ucs_impacts", run_id=s.run_id)
    assessments = store.rows("ucs_drift_assessments", run_id=s.run_id)
    gh = None if s.dry_run else GitHub(github_token(s))
    modes: dict[str, str] | None = None
    rows = []
    for p in (p for p in proposals if p["action"] in ("pin_bump", "edit")):
        skill, folder = p["forge_skill"], ctx.skill_dir(p["forge_skill"])
        plan = json.loads((folder / "plan.json").read_text()) if (folder / "plan.json").is_file() else {}
        deleted = json.loads((folder / "deleted.json").read_text()) if (folder / "deleted.json").is_file() else []
        files = {f.relative_to(folder / "files").as_posix(): f.read_bytes()
                 for f in (folder / "files").rglob("*") if f.is_file()} if (folder / "files").is_dir() else {}
        files.update({path: None for path in deleted})
        note = ("> **Dry run.** forge_ucs_drift is running with `dry_run=true`, so this pull request wasn't opened. "
                "This is the text it would have.\n") if s.dry_run else ""
        text = publish.body(p, [i for i in impacts if i["forge_skill"] == skill],
                            [a for a in assessments if a["forge_skill"] == skill], plan, s.upstream_repo,
                            diff_stat((folder / "changes.diff").read_text()), note)
        (folder / "pr.md").write_text(text)
        row = {"run_id": s.run_id, "forge_skill": skill, "branch": publish.branch(skill), "pr_number": None,
               "pr_url": None, "commit_sha": None}
        if s.dry_run:
            rows.append({**row, "status": "dry_run", "message": f"{folder / 'pr.md'}"})
            continue
        head = gh.branch_head_message(s.forge_repo, row["branch"])
        if head is not None and not head.startswith(COMMIT_PREFIX):
            rows.append({**row, "status": "held", "message": "A person committed to this branch; it wasn't overwritten."})
            continue
        if modes is None:
            modes = gh.tree_modes(s.forge_repo, run["forge_sha"])
        commit = gh.commit_files(
            s.forge_repo, row["branch"], run["forge_sha"],
            {path: None if content is None else (content, modes.get(path, "100644")) for path, content in files.items()},
            f"{COMMIT_PREFIX}: {skill} for upstream {p['to_tag']}\n\nRun {s.run_id}. {p['summary']}")
        pr = gh.upsert_pull(s.forge_repo, row["branch"], s.forge_branch, publish.title(p), text, draft=not p["valid"],
                            labels=publish.labels(p),
                            comment=f"Updated by forge_ucs_drift run {s.run_id} for upstream {p['to_tag']}.")
        rows.append({**row, "status": "published", "pr_number": pr["number"], "pr_url": pr["html_url"],
                     "commit_sha": commit, "message": ""})
    store.delete("ucs_drift_prs", s.run_id)
    store.append("ucs_drift_prs", rows)

    unfinished = [p for p in proposals if p["action"] == "unassessed"]
    status = "completed_with_errors" if unfinished else "completed"
    message = (f"{len(rows)} pull request(s) {'drafted' if s.dry_run else 'published'}; "
               f"{len(unfinished)} skill(s) not assessed (they're retried on the next run).")
    store.append("drift_runs", [{"run_id": s.run_id, "at": now(), "status": status, "dry_run": s.dry_run,
                                 "forge_sha": run["forge_sha"], "latest_tag": run["latest_tag"], "message": message}])
    print(message)


STEPS = {"detect": detect, "analyze": analyze, "propose": propose, "publish": publish_step}


def run(step: str, settings: Settings) -> None:
    ctx = Context(settings)
    for name in (STEPS if step == "all" else [step]):
        print(f"── {name} ({datetime.datetime.now(datetime.timezone.utc):%H:%M:%S} UTC)")
        STEPS[name](ctx)
