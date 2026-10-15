# Databricks Skill Forge

Use-case skills that let coding agents build complete Databricks solutions, not just individual features.

The [Databricks Agent Skills](https://github.com/databricks/databricks-agent-skills) teach an agent one platform feature at a time: AI Functions, AI Search, Apps, Jobs, and so on. Real projects combine many of them, and agents often struggle to break a high-level goal into the right sequence of steps. A **use-case skill** does that decomposition. It takes the agent from one input to a deployed, evaluated solution through ordered modules, and it loads the base skill each step needs at the point it's needed.

Each use-case skill provides:

- **Ordered modules** that each end with a **Verify** step the agent must pass before moving on.
- **Tested templates and scripts** for the parts that are easy to get wrong, so the agent renders them instead of hand-writing them.
- **Decision points** with a default, and the conditions for switching away from it.
- **Guardrails**: the agent asks before anything billable or shared, and never deletes a resource without confirmation that names it.
- **Evaluation thresholds** that define "done".

## Available skills

| Skill | Version | What it builds |
|---|---|---|
| [`doc-qa-agent`](use-case-skills/doc-qa-agent/SKILL.md) | 0.2.0 | A document Q&A (RAG) agent that cites its sources, over the files in a Unity Catalog volume (PDF, DOCX, PPTX, images). It uses `ai_parse_document` and `ai_prep_search` ingestion, an AI Search index, a LangGraph agent on Databricks Apps (Agent Runtime), and MLflow evaluation. Default model: Claude Sonnet 5.5 (`system.ai.claude-sonnet-5-5`). |

Next up: a structured document extraction skill.

## Requirements

- Databricks CLI v1.11.0 or later, with an authenticated profile. The install steps below were verified with v1.20.0.
- Python 3.11+ and [uv](https://docs.astral.sh/uv/).
- The Databricks Agent Skills (v0.2.27 or later), installed as skill folders in the same skills directory as the use-case skill.
- A Unity Catalog workspace with serverless compute, and permission to create jobs, Databricks Apps, and AI Search endpoints (or use an existing one). `doc-qa-agent` checks the exact privileges during preflight.

Each skill installs anything else it needs during preflight. For example, `doc-qa-agent` installs the Agent Bricks CLI (`databricks-agentbricks`).

## Install

Put the base skills and the use-case skill side by side in your agent's skills directory. These commands use `.claude/skills/` (Claude Code, project scope). For other agents, use their skills directory, such as `.agents/skills/`.

```bash
git clone https://github.com/bendoan-db/databricks-skill-forge.git

# 1. Base skills, written as skill folders
databricks aitools install --path .claude/skills

# 2. The use-case skill, next to them
cp -R databricks-skill-forge/use-case-skills/doc-qa-agent .claude/skills/

# 3. Confirm every base skill the use-case skill needs is present and new enough
uv run .claude/skills/doc-qa-agent/scripts/check_base_skills.py
```

Use `--path`. Without it, `databricks aitools install` sets up a plugin for Claude Code, Codex, and GitHub Copilot, and `check_base_skills.py` looks for the base skills next to the use-case skill, not inside a plugin. Restart your agent after installing so it picks up the new skills.

## Use

Ask your agent in plain language:

> Build a document Q&A agent over the PDFs in /Volumes/main/legal/contracts and deploy it as a Databricks App.

`doc-qa-agent` runs six modules in order:

| # | Module | Result |
|---|---|---|
| 00 | Preflight | Inputs resolved, tooling and base skills checked, your go-ahead on cost |
| 01 | Ingest and parse | A bundle-managed job that parses the volume's files and reruns when new files arrive |
| 02 | Chunk and index | Search chunks with document-level metadata, served by an AI Search index |
| 03 | Agent | A LangGraph agent that answers with `[file_name, p. N]` citations, tested locally |
| 04 | Deploy app | The agent running on Databricks Apps as `agent-bricks-<name>` |
| 05 | Evaluate | An MLflow evaluation run, gated on groundedness ≥ 0.8, relevance ≥ 0.8, and correctness ≥ 0.6 |

The agent asks before it starts anything billable:
- parsing (charged per page);
- creating an AI Search endpoint (charged while it exists);
- deploying the app.

It shares the app only with the people you name. Resolved settings are saved to `doc-qa.json` in the working directory, so a later session can resume where the last one stopped. When it's done, the working directory holds two projects: `<name>-ingest/` (the ingestion bundle) and `<name>/` (the Agent Bricks agent).

## Repository layout

```
databricks-skill-forge/
├── use-case-skills/       # one folder per use-case skill; each folder is what gets installed or published
│   └── doc-qa-agent/
│       ├── SKILL.md       # entry point: inputs, guardrails, workflow, decision points
│       ├── modules/       # one file per step; each ends with a Verify gate
│       ├── scripts/       # templates and helpers the agent renders, copies, or runs
│       ├── evals/         # end-to-end certification scenarios and routing prompts
│       └── ucs.yaml       # manifest: version, base-skill dependencies, known gaps, tracked platform facts
├── docs/                  # maintainer notes; not part of any published skill
└── tests/                 # checks that run without a Databricks workspace
```

## Contributing

`doc-qa-agent` is the reference implementation. A new use-case skill should follow its shape and these rules:

- **Point to base skills; don't restate them.** A step that needs platform detail names the base skill and reference file in a **Load:** line. Base skills aren't loaded up front.
- **Gate every module.** Each module ends with a Verify step that can actually fail.
- **Render, don't hand-fill.** Values resolved during the workflow go into a state file, and templates are rendered from it by a script that validates the result.
- **Docs win over base skills.** When a base skill is out of date, add a one-line override to SKILL.md, record the gap with doc evidence in `ucs.yaml` (`coverage_gaps`), and draft an upstream change proposal in `docs/`.
- **Track hard-coded facts.** Platform limits and defaults a skill depends on go in `ucs.yaml` (`facts`), with a source and the files that use them.
- **Stay within skill limits:** at most 50 files or folders, at most 3 levels deep, text files only, and a frontmatter description of at most 1,024 characters.
- **Keep maintainer rationale in `docs/`,** not in the skill. Anything in the skill folder can be loaded into an agent's context.

Run the checks before opening a pull request. They cover the SQL template placeholders, the rendering and base-skill check scripts, manifest-to-module consistency, eval thresholds, and the skill folder limits:

```bash
uv run --no-project --with pytest --with pyyaml pytest -q -p no:cacheprovider tests
```

The end-to-end scenarios in `evals/scenarios.yaml` need a Databricks workspace.

## Roadmap

Forge is in early development. The following are planned but not yet in this repository:

- **Base skill refinement:** a Databricks workflow that refreshes the base skills monthly from release notes and documentation, with every change backed by a doc citation.
- **Drift checks:** rerunning each use-case skill's scenarios when the base skills change, then opening a pull request (compatible change) or an issue (breaking change).
- **Use-case skill builder:** a skill that helps you combine base skills into your own use-case skill.
- **Customer and domain skills:** context about your workspace and environment that tailors a use-case skill to it.
- **Unity Catalog installer:** publishes the skills as governed Unity Catalog skills, so agents across your organization, including Genie Code, can use them.
