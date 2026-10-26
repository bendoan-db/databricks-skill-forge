# forge_ucs_drift design notes

Maintainer notes for `jobs/forge_ucs_drift`: the daily Databricks job that watches [databricks-agent-skills](https://github.com/databricks/databricks-agent-skills) releases and proposes Forge skill updates as pull requests. It's the charter's "Drift handling" flow (U7–U9). It compares against upstream tags, because Forge base releases (`dbs-YYYY.MM`) don't exist yet; when they do, only `detect` changes. Nothing here is published or installed.

## Where it runs

- **Workspace:** `fevm-fins-skill-forge.cloud.databricks.com`, through the CLI profile `forge`. The job runs as whoever deploys the bundle.
- **State:** `fins_skill_forge_catalog.forge_drift`, with the tables below and the volume `drift`.
- **GitHub:** a personal access token in the secret scope `forge`, key `git-pat`. It needs Contents and Pull requests write on `bendoan-db/databricks-skill-forge`.
- **Models:** `system.ai.claude-sonnet-5-5` decides whether a change matters; `system.ai.claude-opus-5-5` writes the edits.
- **Schedule:** daily at 10:00 UTC, about 90 minutes after upstream usually publishes.
- **Trial:** `dry_run` defaults to `true`. For the first one to two weeks, runs write each pull request's text to the volume and open nothing. To go live, deploy with `--var dry_run=false`.

## The four tasks

| Task | Does | LLM | Token |
|---|---|---|---|
| `detect` | Downloads Forge `main` and every upstream tree it needs (the newest release, plus each skill's pin) into the volume, and records new releases. Skips the run when neither moved since the last completed run in the same mode | No | Reads |
| `analyze` | Builds the dependency graph, finds the impacts from each skill's pin to the newest release, and asks the assess model about each impact that isn't already decided | Sonnet | No |
| `propose` | Decides per skill, writes and applies the edit (or just moves the pin), and runs the repo's own checks on the result, with up to two attempts | Opus | No |
| `publish` | Builds each pull request's text; in a live run, commits to `drift/<skill>` and opens or updates the pull request | No | Writes |

**Dependencies.** A module depends on the files its **Load:** lines name: a reference file, or `SKILL.md` when the line names a skill alone. "Read only the file named" means a change to that skill's other files doesn't matter. `coverage_gaps` entries depend on the stale file they quote. The `forge` router depends on the skills it falls back to or names in its description existing, and keeping their descriptions. It's compared from the newest pin, because it has no pin of its own.

**Impacts** are computed per file, by content hash. Upstream often changes a skill without bumping its version, and a `SKILL.md` hash leaves out the version line, so a bump alone isn't a content change.

| Impact | Severity |
|---|---|
| A base skill is removed or renamed; a cited file is removed | breaking, whatever the assess model says |
| A cited file, or a `SKILL.md` that's read, changed; a coverage gap's file changed; a router skill's description changed | assessed by the model |
| Only the version moved | compatible, without asking the model |

**Decisions**, per Forge skill, from the worst finding:

| Materiality | Action |
|---|---|
| none | Record it; no pull request |
| compatible | Pull request moving the pin, plus a patch version bump |
| material, breaking | The edit model rewrites the skill; the pipeline sets the version (the model's bump level) and the pin |
| not assessed (the model failed) | Recorded. The run ends `completed_with_errors`, which isn't a completed run, so the next day retries |

## Checking a proposal

`propose` works on a copy of `main` in the task's temporary storage:

1. **Apply.** The model's edits are exact-match replacements, creates, and deletes, limited to `skills/<skill>/` and `README.md`. Any other path is refused. Each `old` text must match exactly once.
2. **Pipeline-owned changes.**
   - Shared files are copied to every skill that carries them.
   - The pin and version are set, the README row too.
   - `tools/build_forge_catalog.py` regenerates the router's table.
3. **Checks**, run in a child process whose environment has only `PATH` and the locale (no Databricks or GitHub credentials):
   - The repo's tests. A test that also fails on unchanged `main` here doesn't count against the proposal: `validate.baseline` runs `main` first.
   - The catalog check.
   - `check_base_skills.py` against the newest release's skills, which now also checks every cited reference file exists.
4. **Retry.** On failure, the failures are fed back for one more attempt. If it still fails, `publish` opens a draft pull request labeled `needs-owner`.

**Script edits are allowed (decided 2026-10-26).** So the checks run code the model wrote. Mitigations:
- `propose` never reads the token; only `detect` and `publish` do, and the bundle test enforces it.
- The checks' process has no credentials in its environment.
- Added lines in code that touch the network, commands, or credentials are listed under "Review carefully" in the pull request.
- The token is a fine-grained PAT limited to this one repository.
- Every pull request needs a person's review: never auto-merge.

**What remains:** code in the checks still runs inside the job's serverless environment as the job's identity. Treat each upstream release as untrusted input, and don't widen the token.

## Pull requests

- **One per Forge skill,** on `drift/<skill>`. A later run force-moves the branch and updates the pull request, with a comment.
- **Your edits are safe.** If the branch's head commit wasn't made by the pipeline (its message doesn't start with `forge_ucs_drift`), a person has worked on it, so the run records `held` and leaves the branch alone.
- **Labels:** `drift`, `drift:<materiality>`, and `needs-owner` when the checks failed.
- **Body:** the triggering upstream compare link, the impacts, the assessment, each edit's rationale and evidence, the files, the validation, "Review carefully", and a draft upstream change proposal when Forge keeps an override. That proposal is never filed.
- **Not certified.** The certification scenarios (`evals/scenarios.yaml`) don't run, and the pin comment says "not yet certified in a workspace".

## Tables and files

All tables are append-only and keyed by `run_id`. A retried task replaces its own rows for that run.

- **Runs and releases:** `drift_runs` (one row per state: `detected`, `skipped`, `completed`, `completed_with_errors`); `upstream_releases`; `upstream_skills`, the per-tag frontmatter and file hashes.
- **Per run:** `ucs_dependencies`, `ucs_impacts`, `ucs_drift_assessments` (with the model and prompt version), `ucs_drift_proposals`, `ucs_drift_prs`.
- **Volume:**
  - `upstream/<tag>/`, `forge/<sha>/`;
  - `runs/<run_id>/<skill>/`: `diffs/`, `assessments/`, `plan.json`, `changes.diff`, `files/`, `validation.txt`, and `pr.md`.

## Operating it

```bash
cd jobs/forge_ucs_drift
databricks bundle deploy                                   # creates the schema, volume, and job
databricks bundle run forge_ucs_drift                      # a run now
databricks bundle run forge_ucs_drift --params force=true,skills=forge-doc-qa-agent
databricks bundle deploy --var dry_run=false               # after the trial
```

Local development needs no workspace: it uses public GitHub reads and no LLM.

```bash
uv run --no-project --with pyyaml --with pytest --with pypdf --with rapidfuzz --with scipy \
  python -B jobs/forge_ucs_drift/src/run.py all --run-id dev --local-dir /tmp/forge-drift --no-llm
```

**During the trial,** read `runs/<run_id>/<skill>/pr.md` and `changes.diff`, and compare the decisions in `ucs_drift_assessments` and `ucs_drift_proposals` with your own. Prompts are versioned (`llm.PROMPT_VERSION`); bump the version when you change one.

## Not yet verified

- **`ai_query` on these models.** That `system.ai.claude-sonnet-5-5` and `system.ai.claude-opus-5-5` accept `max_tokens` 32,000, and return within the task timeout for prompts this size.
  - **No `responseFormat`.** The second run (2026-10-26) got `INVALID_PARAMETER_VALUE` on every row: "Structured output is not supported for this model because its translation requires forced tool use, which newer Anthropic models reject." So the job doesn't set it. The prompts ask for one JSON object, and `llm.parse_json` finds it, fenced or not. Upstream's `databricks-ai-functions` still recommends `responseFormat => '{"type":"json_object"}'` without noting this.
  - **The `failOnError => false` struct.** The first run (2026-10-26) failed in `analyze`: the struct had no `response` field, which the ai_query reference names. Upstream's examples use `result`, and so does `forge-unstructured-doc-extraction`'s `prep_chunks.sql`. `llm.struct_fields` now takes `result` (or `response`) and `errorMessage`, checked on the query's schema before any model is called. Which name the runtime has is confirmed by the next run.
  - **Serverless auto-optimization retries failed tasks.** That run's `analyze` ran three times, and each attempt called the assess model for every impact. The job doesn't turn this off; the next day's run already retries a run that didn't complete.
- **The workspace setup:**
  - serverless egress to `api.github.com` and `codeload.github.com`;
  - volume support for the many small files the trees and artifacts write;
  - `/bin/bash` and `curl` in the serverless image, which the installer tests run under. If they're missing there, those tests fail on `main` too and are ignored.
- **The live GitHub writes:** Git Data API commits, force-moving the branch, labels, and comments. The first live run after the trial is the test.
- **The PowerShell installer tests' stand-in,** changed for the reference-file check. It runs in GitHub Actions, not locally.
