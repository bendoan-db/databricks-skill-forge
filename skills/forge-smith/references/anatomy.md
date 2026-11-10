# Anatomy of a Forge use-case skill

Read this before designing a skill. `check_skill.py` enforces the rules marked (checked); the rest takes judgment.

## What a use-case skill is

- **One end result,** such as a search index, an agent, typed tables, a dashboard, an app, or a model. The skill takes an agent from the user's request to that result, deployed and evaluated. The `forge` router picks a skill by its result, so two skills with the same result compete.
- **Glue, not platform docs.** Each step loads the base skill that covers the platform details, and the skill adds only what this use case needs: the order, the settings, the checks, and the scripts that are easy to get wrong.
- **Done is measured.** The evaluate module's thresholds define done.

## Folder

```
<name>/
├── SKILL.md              # the orchestrator
├── ucs.yaml              # manifest: dependency graph, inputs, catalog entry, facts, gaps
├── modules/NN-*.md       # the steps, in order: 00-preflight first, and an NN-evaluate module
├── scripts/              # what the agent runs or renders: check_base_skills.py, write_readme.py, evaluate.py, the ported code (job code as notebooks)
└── evals/scenarios.yaml  # end-to-end certification scenarios
```

At most 50 files and folders, 3 levels deep, text files only, and no caches (checked).

## SKILL.md

- **Frontmatter** (checked): `name` (the folder's name), `description` (up to 1024 characters: what it builds, and when to use it), `compatibility` (up to 500), `metadata.version`, and `metadata.forge_kind: use-case`. In a Forge checkout, the description starts "Forge use case, normally started by the forge skill:", and the router's description carries the "Prefer it over databricks-…" wording.
- **Sections, in order** (checked): Reference architecture, Conventions, Inputs, Guardrails, Workflow, Decision points, Done when, Base-skill guidance to override.
- **The project README** (checked). The Workflow section runs `uv run <skill>/scripts/write_readme.py --state <workdir>/<state file>` each time a module's Verify passes, module 00's included, and its working-directory tree lists `README.md`. The script writes `<workdir>/README.md` for whoever opens the folder later: what was built, its settings, and each module's status. It builds the README from this skill's own text: the overview, Reference architecture, Inputs table, Workflow checklist, table, and tree, Decision points, Done when, and resume table, and each module's purpose, Inputs → outputs, step headings, and other sections. Write those for a person reading the project, too.
- **Keep it compact.** Rationale and history go in the repo's `docs/`, not the skill.

## Modules

Each module (checked): `# NN · Title`, a one-line purpose, `## Inputs → outputs` (not module 00), numbered steps (`## 1. …`), `## Verify`, and a Troubleshooting table. Optionally, **Docs:** links for areas no base skill covers.

- **Load lines.** `**Load:** \`databricks-x\` \`references/y.md\` (what for).`, at the step that needs it, never up front. Name one reference file; the agent reads only that file. A skill named alone means its SKILL.md. References used only for troubleshooting go in Troubleshooting rows. A module's Load lines name exactly its `base_skills` in ucs.yaml, and so does its Workflow row (checked).
- **State.** Every resolved value goes in one state file in `<workdir>`, and modules refer to it as `<key>`. Module 00 writes it; each module reads it first and records what it adds.
- **Commands** use `--profile <profile>` and state keys, never tokens or hard-coded hosts.
- **Verify** lists observable checks: a row count, a query result, an endpoint that answers, a URL.
- **Billable or shared actions need confirmation,** such as AI functions over the data, endpoints, apps, clusters, jobs that run, and grants. A delete needs its own confirmation that names the resource.

## Module 00: preflight

1. **Ask for the requirements** in one question, and wait: the required inputs, each with what it's for and its format. Confirm answers the user already gave to `forge`.
2. **Collect the other inputs:** propose defaults, and show the derived values.
3. **Tooling and base skills:** `check_base_skills.py`.
4. **Check what the answers name:** each named resource exists and the user can use it. When the answer can be "create one", check the user can create it; if they can't, they must name an existing one before you continue.
5. **Permissions:** report what's missing; never grant it.
6. **Cost go-ahead:** what each step bills, then an explicit yes.
7. **Write the state file.**

## Scripts

- **Ported code** becomes job code the skill deploys, scripts the agent runs (`uv run <skill>/scripts/x.py --state <workdir>/<state file>`), or templates it renders from the state file. Every setting comes from the state file. No catalog, schema, path, endpoint, or host is hard-coded.
- **Job and training code is a Databricks notebook** (checked), so users can open, run, and edit it in their workspace. `templates/notebook.py` is the skeleton:
  - line 1 is `# Databricks notebook source`, and `# COMMAND ----------` separates the cells;
  - a `# MAGIC %md` cell says what it does, a `# MAGIC %pip install -q "<spec>"` cell installs what the serverless environment lacks, and the docstring cell follows;
  - every setting is a flag in `build_parser()`; `notebook_argv()` reads the same settings from widgets;
  - the last cell is the guarded run cell, so tests can import it, and it still runs locally through Databricks Connect.
  The bundle template runs it as a `notebook_task` (`notebook_path: src/<file>.py`), with its flags as `base_parameters` named like each flag's dest. Never a `spark_python_task`.
- **Users' workspace edits survive** (checked). `scripts/render.py` (from `templates/render.py`) copies the job's notebooks into the bundle with a checksum line, keeps a copy the user changed and lists it under `kept_edited`, and replaces it only with `--force`. The module that deploys the job has an `## Editing the job's notebooks` section: where the notebooks are in the workspace, and how to export an edit back into the bundle before the next deploy.
- **Porting a source notebook** into the skeleton:
  - `.ipynb` cells, or a source notebook's cells, go into the stub's cells, grouped by stage;
  - `dbutils.widgets` become flags in `build_parser()`, which the stub's `notebook_argv()` reads back from widgets;
  - `%pip install` lines go in the `%pip` cell, pinned; `%run` helpers are copied into the notebook, since skills can't import each other;
  - `%sql` cells become `spark.sql(...)`, or sections of a `.sql` file the notebook reads from `script_dir()`;
  - `display()` calls and exploration cells are dropped, and the run ends by printing a JSON summary;
  - the Spark session comes from `get_spark()`, so the notebook also runs locally through Databricks Connect, and Python UDFs stay defined in the notebook.
- **App code stays a Python module:** a Databricks App's or an agent's code is imported and packaged by the app, which is the platform's practice. List it under a module's `app_scripts`.
- **The agent's local tools** (`render.py`, `evaluate.py`, `check_base_skills.py`, `write_readme.py`) stay scripts, with PEP 723 headers declaring their dependencies, for Python 3.11 or later.
- **Jobs run on serverless** unless something needs classic compute. Then say what needs it, as a decision point.
- **`check_base_skills.py` and `write_readme.py` are shared.** Copy them unchanged, and list them under `shared` in ucs.yaml (checked).
- **Reruns are safe.** Process new or changed data only, and don't re-bill AI functions for data already done.

## ucs.yaml

`name`, `version`, `owner`, `upstream` (the databricks-agent-skills tag written against), `upstream_min`, `source` (where it was built from), `inputs` (`required`, `optional`), `shared`, `catalog` (`title`, `choose_when`, `not_for`, `produces`, `state_file`, `next`), `requires` (use-case skills run first), `modules` (`id`, `file`, `scripts`, `base_skills` with `name`, `version`, `uses`, and `external` packages), `coverage_gaps`, `facts`, and `scenarios`.

## Values that must agree

- Load lines, ucs.yaml `base_skills`, and the Workflow row (checked).
- SKILL.md `metadata.version` and ucs.yaml `version` (checked).
- `check_base_skills.py` and `write_readme.py`, the copies in `templates/`, and ucs.yaml `shared` (checked).
- `evaluate.py` THRESHOLDS and scenarios.yaml `defaults.thresholds` (checked), and the copies in Done when and the evaluate module.
- Inputs: ucs.yaml `inputs`, the SKILL.md Inputs table, and module 00 (checked).
- State keys: the module text and the scripts.
- Facts: each ucs.yaml `facts` entry, and every file in its `used_in` (checked: the files exist).
- Stale base guidance: each "Base-skill guidance to override" line has a `coverage_gaps` entry with the doc page that shows it.

## Designing from a codebase

- **One skill per end result.** Stages become modules. Techniques and variants become decision points or optional inputs, not skills of their own. For example, advance-document-processing's seven folders became two skills: its stages became modules, and its long-PDF and split-page recipes became options of one shared parse stage.
- **Reuse before you add.** If an installed Forge skill already produces the result, extend it. If it produces one stage, `requires` it.
- **Shape:** 00-preflight, two to six working modules, then the evaluate module. Each module has one verifiable output.
- **Improve on the source where it's fragile:**
  - process data incrementally, not by full re-reads that re-bill every run;
  - use serverless compute;
  - use `system.ai` model names;
  - use CLI profiles, not tokens;
  - confirm before billable steps;
  - write steps that are safe to rerun;
  - put missing data in an error column, not a log line.

## What not to port

| In the codebase | In the skill |
|---|---|
| Hard-coded tokens, passwords, workspace URLs | Nothing. Use the CLI profile, and secret scopes by name |
| The author's catalog, schema, paths, and resource names | Inputs |
| Agent Bricks Knowledge Assistant | A RAG agent, as `forge-doc-qa-agent` builds, or `requires` it |
| Agent Bricks Information Extraction | `ai_classify` and `ai_extract`, as `forge-structured-doc-extraction` does |
| Demo data generators, sample outputs, scratch notebooks | Nothing, unless the use case needs synthetic data (then `databricks-synthetic-data-gen`) |
| Legacy model endpoint names (`databricks-claude-…`) | `system.ai` model services, with the legacy name as the fallback where `system.ai` isn't available |
| Copies of platform documentation | A Load line to the base skill that covers it |
