# Databricks Skill Forge

Use-case skills that let coding agents build complete Databricks solutions, not just individual features.

The [Databricks Agent Skills](https://github.com/databricks/databricks-agent-skills) teach an agent one platform feature at a time: AI Functions, AI Search, Apps, Jobs, and so on. Real projects combine many of them, and agents often struggle to break a high-level goal into the right sequence of steps. A **use-case skill** does that decomposition. It takes the agent from one input to a deployed, evaluated solution through ordered modules, and it loads the base skill each step needs at the point it's needed.

You start every build the same way: invoke the **`forge`** skill and say what you want. `forge` matches the request to one use-case skill and hands off to it. When no use case fits, it points you to the base skill that covers the request.

Each use-case skill provides:

- **Ordered modules** that each end with a **Verify** step the agent must pass before moving on.
- **Tested templates and scripts** for the parts that are easy to get wrong, so the agent renders them instead of hand-writing them.
- **Decision points** with a default, and the conditions for switching away from it.
- **Guardrails**: the agent asks before anything billable or shared, and never deletes a resource without confirmation that names it.
- **Evaluation thresholds** that define "done".

## How to Run

1. **Sign in to your workspace** with the Databricks CLI, and make sure `uv` is installed (see [Requirements](#requirements)):

   ```bash
   databricks auth login --host https://<your-workspace>.cloud.databricks.com --profile DEFAULT
   ```

2. **Install** the base skills, the Databricks MCP server, and the Forge skills. Run this in the project folder where you use your agent; there's nothing to clone (see [Install](#install) for other agents and options):

   ```bash
   bash <(curl -fsSL https://github.com/bendoan-db/databricks-skill-forge/releases/latest/download/install.sh)
   ```

   On Windows, in PowerShell:

   ```powershell
   irm https://github.com/bendoan-db/databricks-skill-forge/releases/latest/download/install.ps1 | iex
   ```

   For Genie Code, in a notebook or the Genie Code CLI, use the notebook installer instead (see [Genie Code](#genie-code)).

3. **Restart your agent**, then invoke `forge` and say what you want built (in Genie Code, type `@forge`):

   ```
   /forge build a document Q&A agent over the PDFs in /Volumes/main/legal/contracts
   /forge build a search index over /Volumes/main/hr/policies that our app can query
   /forge pull the invoice number, vendor, and total from /Volumes/main/ap/invoices into tables
   ```

   Type just `/forge` to see the use cases.

4. **Answer the agent's questions.** `forge` tells you which use case it picked, and that skill asks for what it needs, such as where the documents are and which AI Search endpoint to use. It asks before anything billable or shared, and shows you results at each step. Its progress is saved in the project folder, so you can run `/forge` there again later to pick up where you left off.

## Available skills

| Skill | Version | What it builds |
|---|---|---|
| [`forge`](skills/forge/SKILL.md) | 0.1.0 | The entry point. It matches your request to one of the use-case skills below and hands off to it, resumes a build already in the working directory, and points to a base skill when no use case fits. It builds nothing itself. |
| [`forge-unstructured-doc-extraction`](skills/forge-unstructured-doc-extraction/SKILL.md) | 0.1.0 | An AI Search index over the files in a Unity Catalog volume (PDF, DOCX, PPTX, images), for an agent or app to query. It parses with `ai_parse_document`, chunks with `ai_prep_search` (document-level metadata filters, optional chart readings by a vision model), keeps the index fresh as files arrive, and scores retrieval (document and page hit rates) in MLflow. |
| [`forge-doc-qa-agent`](skills/forge-doc-qa-agent/SKILL.md) | 0.4.0 | A document Q&A (RAG) agent that cites its sources: a LangGraph agent on Databricks Apps (Agent Runtime) that searches a `forge-unstructured-doc-extraction` index, evaluated with MLflow judges. It runs `forge-unstructured-doc-extraction` first when the documents aren't indexed yet. Default model: Claude Sonnet 5.5 (`system.ai.claude-sonnet-5-5`). |
| [`forge-structured-doc-extraction`](skills/forge-structured-doc-extraction/SKILL.md) | 0.2.0 | Typed Delta tables of the fields in a volume's documents, with a confidence score and citation for every value. It classifies mixed documents and splits multi-document packets (loan or claim files) with `ai_classify`, extracts per document type with `ai_extract`, keeps a review queue of low-confidence values, scores accuracy against labeled documents in MLflow, and can find word-level boxes for each value with OCR. |

`forge-unstructured-doc-extraction` and `forge-structured-doc-extraction` parse with the same shared stage, which handles PDFs over the 500-page `ai_parse_document` limit and scans split into one file per page.

## Requirements

- Databricks CLI v1.11.0 or later, with an authenticated profile. The installer was tested with v1.20.0. Some agents need newer versions for AI Dev Kit to target them: Gemini CLI needs v1.12.0, and Kiro needs v1.20.0.
- Python 3.11+ and [uv](https://docs.astral.sh/uv/).
- macOS or Linux with `bash`, `curl`, and `tar`; or Windows 10 (1803) or later with PowerShell 5.1 or 7.
- The Databricks Agent Skills (v0.2.27 or later), as skill folders in the same skills directory as `forge` and the use-case skills. The installer sets this up.
- A Unity Catalog workspace with serverless compute, and permission to create jobs. `forge-unstructured-doc-extraction` also needs AI Search endpoints (or an existing endpoint), and `forge-doc-qa-agent` needs Databricks Apps. Word-level localization in `forge-structured-doc-extraction` needs a classic job cluster. Each skill checks the exact privileges during preflight.

Each skill installs anything else it needs during preflight. For example, `forge-doc-qa-agent` installs the Agent Bricks CLI (`databricks-agentbricks`).

## Install

Run the installer in the project folder where you use your agent, or pass `--global` to install for your user in every project. It downloads the latest release, so there's nothing to clone:

```bash
bash <(curl -fsSL https://github.com/bendoan-db/databricks-skill-forge/releases/latest/download/install.sh)                                 # Claude Code, this project
bash <(curl -fsSL https://github.com/bendoan-db/databricks-skill-forge/releases/latest/download/install.sh) --tools claude,codex --dry-run  # see the plan first
```

On Windows, in PowerShell:

```powershell
irm https://github.com/bendoan-db/databricks-skill-forge/releases/latest/download/install.ps1 | iex
# With options:
& ([scriptblock]::Create((irm https://github.com/bendoan-db/databricks-skill-forge/releases/latest/download/install.ps1))) -Tools claude,codex -DryRun
```

To pin a version, replace `latest/download` with `download/v0.1.0`. To read the script before running it, download it first (`curl -fsSLO …/install.sh`), then run `bash install.sh`.

It shows its plan, asks once, and then:

1. **Runs [AI Dev Kit](https://github.com/databricks-solutions/ai-dev-kit) v0.2.0.**
   - It installs every stable Databricks base skill through `databricks aitools`, plus the MLflow skills.
   - It registers the Databricks MCP server with each agent. The server runs from a copy of AI Dev Kit in `~/.databricks-skill-forge/ai-dev-kit`.
2. **Copies the Forge skills** (`forge` and the use-case skills) into the same skills directories, and removes Forge skills installed under their names from before the `forge-` prefix.
3. **Checks** that each use-case skill finds every base skill it needs.

Every download is checked before anything is installed. The release's skills archive is checked against a checksum written into that release's installer, and AI Dev Kit's archive against a pinned checksum. A mismatch stops the install.

| Option (bash) | Option (PowerShell) | Effect |
|---|---|---|
| `-g`, `--global` | `-Global` | Install for your user, in every project |
| `-d`, `--dir DIR` | `-Dir DIR` | Install into this project instead of the current directory |
| `-t`, `--tools LIST` | `-Tools LIST` | Agents to install for (default `claude`): `claude`, `codex`, `copilot`, `cursor`, `gemini`, `antigravity`, `opencode`, `kiro`, `windsurf` |
| `-p`, `--profile NAME` | `-Profile NAME` | Databricks CLI profile the MCP server uses (default `DEFAULT`) |
| `--no-mcp` | `-NoMcp` | Skip the MCP server |
| `--forge-only` | `-ForgeOnly` | Install or update only the Forge skills, next to base skills that are already installed |
| `--ai-dev-kit-ref REF` | `-AiDevKitRef REF` | Use another AI Dev Kit release or branch (not checksum-verified) |
| `--dry-run` | `-DryRun` | Print the plan and change nothing |
| `-y`, `--yes` | `-Yes` | Don't ask for confirmation (needed when there's no terminal) |
| `-v`, `--version` | `-Version` | Print the installer's version |

The installer requests the base skills by name. AI Dev Kit's default "all skills" install puts them in a plugin for Claude Code, Codex, and Copilot, and Forge needs them as folders in the same skills directory. If that plugin is already installed, the installer says so, and gives the command to remove it, because Claude Code would otherwise see two copies.

Rerun the installer to update. Update only the base skills with `databricks aitools update`. Restart your agent after installing, so it loads the new skills.

**From a clone:** `./install.sh` (or `.\install.ps1`) in a checkout installs that checkout's skills, which is how to test changes before a release.

**Without the installer:** put the base skills in your agent's skills directory with `databricks aitools install --path .claude/skills`, then `cp -R databricks-skill-forge/skills/* .claude/skills/`. The `--path` flag matters: without it, `aitools` installs a plugin instead of folders. Then check each use-case skill with `uv run .claude/skills/<skill>/scripts/check_base_skills.py`. This route doesn't set up the MCP server.

### Genie Code

[Genie Code](https://docs.databricks.com/aws/en/genie-code/skills) loads skills from workspace folders, so it has its own installer, the `install_genie_code` notebook:

1. **Get the notebook into your workspace.** Download `install_genie_code.py` from the [latest release](https://github.com/bendoan-db/databricks-skill-forge/releases/latest) and import it into a workspace folder. Or clone this repository as a [Git folder](https://docs.databricks.com/aws/en/repos/) and open `install_genie_code` there, which installs that checkout's skills.
2. **Run all** on serverless compute, or on Databricks Runtime 14.3 LTS or later. The compute needs outbound access to GitHub. With **Action** set to `plan`, the default, the notebook only shows what it would change.
3. **Set Action to `install`**, and run all again.
4. **Start a new Genie Code chat**, and type `@forge` and your request.

The notebook installs into `/Users/<you>/.assistant/skills`, which only you see. Set **Install for** to `workspace` to install into `/.assistant/skills` for everyone instead, which takes admin rights on that folder. It installs two sets of skills:
- **The base skills:** every stable databricks-agent-skills skill, at the newest tag a Forge skill is pinned to. They come from GitHub without a checksum check, and without their icon files, so every folder is text only.
- **The Forge skills:** checked against the release's checksum.

It skips the MLflow skills and the MCP server, because no Forge skill needs them, and Genie Code adds MCP servers in its own settings.

**The Genie Code CLI** reads skills only from your computer. Copy what the notebook installed with `databricks workspace export-dir /Users/<you>/.assistant/skills ~/.genie/skills --overwrite --profile <profile>`. Or, for one project, run the local installer with `--tools codex --no-mcp`: it writes `.agents/skills/`, which the CLI also reads. The Forge scripts need `uv` on your computer.

**Not yet tried in a notebook:** the use-case skills run their scripts with `uv run` and deploy with the `databricks` CLI. With the Genie Code CLI, both run on your computer. Genie Code in a notebook may have neither, so a build there can stop at those steps.

## Use

Invoke `forge` and say what you want built, for example `/forge build a document Q&A agent over /Volumes/main/legal/contracts`. Type just `/forge` to see the use cases. A plain request without `/forge` ("build a chatbot over the PDFs in my volume") should still start in `forge`; the routing evals check that.

`forge` picks the use case from what you want at the end: an agent that answers questions, a search index, or values in tables. If your request could mean more than one, it asks. It tells you which use case it picked, then the use-case skill takes over. If the working directory already has a build in progress, `forge` offers to resume it.

### Document index

> /forge Parse the PDFs in /Volumes/main/legal/contracts and build a search index our app can query.

`forge-unstructured-doc-extraction` runs four modules in order:

| # | Module | Result |
|---|---|---|
| 00 | Preflight | Inputs resolved, base skills checked, your go-ahead on cost |
| 01 | Ingest and parse | A bundle-managed job that parses the volume's files and reruns when new files arrive |
| 02 | Chunk and index | Search chunks with document-level metadata, served by an AI Search index |
| 03 | Evaluate retrieval | An MLflow run, gated on a document hit rate ≥ 0.9 and a page hit rate ≥ 0.8 |

Before building anything, the agent asks where the documents are, which AI Search endpoint to use, what to name the index, and whether the index must stay current as new files arrive. If you don't name an endpoint and can't create one, it stops and asks you for one. It also asks before it starts anything billable: parsing and chunking (charged per page and token), or creating an AI Search endpoint (charged while it exists). Settings are saved to `doc-index.json` in the working directory, the eval set to `evals.jsonl`, and the job is `<name>-index/`.

### Document Q&A

> /forge Build a document Q&A agent over the PDFs in /Volumes/main/legal/contracts and deploy it as a Databricks App.

`forge-doc-qa-agent` first runs `forge-unstructured-doc-extraction` if the documents aren't indexed yet, then runs four modules in order:

| # | Module | Result |
|---|---|---|
| 00 | Preflight | The index found (or built), inputs resolved, tooling and skills checked, your go-ahead on cost |
| 01 | Agent | A LangGraph agent that answers with `[file_name, p. N]` citations, tested locally |
| 02 | Deploy app | The agent running on Databricks Apps as `agent-bricks-<name>` |
| 03 | Evaluate | An MLflow evaluation run, gated on groundedness ≥ 0.8, relevance ≥ 0.8, and correctness ≥ 0.6 |

The agent asks before deploying the app, and shares it only with the people you name. Its settings are saved to `doc-qa.json`, next to the index's `doc-index.json`, so a later session can resume where the last one stopped. When it's done, the working directory also holds `<name>/`, the Agent Bricks agent.

### Structured extraction

> /forge Our loan files in /Volumes/main/lending/loan_files are single PDFs with many documents inside. Pull the borrower's income and bank balances into tables.

`forge-structured-doc-extraction` runs these modules in order:

| # | Module | Result |
|---|---|---|
| 00 | Preflight | Inputs and layout resolved (one type, one document per file, or packets), base skills checked, your go-ahead on cost |
| 01 | Ingest and parse | A bundle-managed job, and every document parsed once |
| 02 | Classify | Document types designed with you; packets split into sub-documents |
| 03 | Extract | A schema per type; every value extracted with a confidence score and citation |
| 04 | Tables and review | One typed table per document type, child tables for line items, and a review queue |
| 05 | Evaluate | Accuracy against labeled documents in MLflow, gated on classification ≥ 0.95, field accuracy ≥ 0.9, and array soft F1 ≥ 0.8 |
| 06 | Localize (optional) | Word-level boxes for each value, gated on an 80% hit rate |

The agent asks before parsing, extracting, re-extracting, or starting the OCR cluster, and treats extracted values as sensitive in its reports. Settings are saved to `extraction.json` in the working directory, and the job is `<name>-extract/`. After setup, new files in the volume are processed on arrival.

## Repository layout

```
databricks-skill-forge/
├── skills/                # one folder per skill; each folder is what gets installed or published
│   ├── forge/             # router: SKILL.md (use-case table generated from each ucs.yaml), forge.yaml, evals/routing.yaml
│   ├── forge-unstructured-doc-extraction/
│   │   ├── SKILL.md       # entry point: inputs, guardrails, workflow, decision points
│   │   ├── modules/       # one file per step; each ends with a Verify gate
│   │   ├── scripts/       # templates and helpers the agent renders, copies, or runs
│   │   ├── evals/         # end-to-end certification scenarios, each started through forge
│   │   └── ucs.yaml       # manifest: version, catalog entry for forge, base-skill dependencies, shared files, known gaps, tracked facts
│   ├── forge-doc-qa-agent/                # same layout
│   └── forge-structured-doc-extraction/   # same layout
├── docs/                  # maintainer notes; not part of any published skill
├── install.sh, install.ps1  # installers (macOS/Linux, Windows): base skills and MCP server through AI Dev Kit, then Forge
├── install_genie_code.py  # Genie Code installer: a notebook that puts the base and Forge skills in a workspace skills folder
├── VERSION                # the release version; release tags are v<VERSION>
├── .github/workflows/     # tests on every pull request; a v* tag also builds and publishes the release
├── jobs/forge_ucs_drift/  # daily Databricks job: proposes skill updates when the base skills change; not published
├── tools/                 # maintainer scripts (forge catalog generator, release builder); not published
└── tests/                 # checks that run without a Databricks workspace
```

## Contributing

`forge-unstructured-doc-extraction`, `forge-doc-qa-agent`, and `forge-structured-doc-extraction` are the reference implementations. A new use-case skill should follow their shape and these rules:

- **Point to base skills; don't restate them.** A step that needs platform detail names the base skill and reference file in a **Load:** line. Base skills aren't loaded up front.
- **Gate every module.** Each module ends with a Verify step that can actually fail.
- **Render, don't hand-fill.** Values resolved during the workflow go into a state file, and templates are rendered from it by a script that validates the result.
- **Docs win over base skills.** When a base skill is out of date, add a one-line override to SKILL.md, record the gap with doc evidence in `ucs.yaml` (`coverage_gaps`), and draft an upstream change proposal in `docs/`.
- **Track hard-coded facts.** Platform limits and defaults a skill depends on go in `ucs.yaml` (`facts`), with a source and the files that use them.
- **Stay within skill limits:** at most 50 files or folders, at most 3 levels deep, text files only, and a frontmatter description of at most 1,024 characters.
- **Keep maintainer rationale in `docs/`,** not in the skill. Anything in the skill folder can be loaded into an agent's context.
- **Share by copying.** Each skill folder is published on its own, so skills can't import each other. Files they share, such as the parse stage (`scripts/parse.py`, `scripts/parse.sql`), are listed under `shared` in `ucs.yaml` and must stay identical; the tests fail when they drift.
- **Register it with `forge`.** Add a `catalog` block to its `ucs.yaml` (title, when to choose it, what it isn't for, what it produces, its state file, next steps), add it to `use_cases` in `skills/forge/forge.yaml`, run `uv run tools/build_forge_catalog.py`, and add routing cases to `skills/forge/evals/routing.yaml`. Its description should say it's normally started by `forge`, so the two don't compete.
- **Build on another use-case skill through its state file.** `forge-doc-qa-agent` lists `forge-unstructured-doc-extraction` under `requires` in `ucs.yaml`, runs it when needed, and reads its `doc-index.json` without writing to it.

Run the checks before opening a pull request. They cover the SQL template placeholders, the rendering, parsing, scoring, and base-skill check scripts, shared files, manifest-to-module consistency, eval thresholds, the skill folder limits, the forge catalog and routing cases, and the installer (with stand-in commands, offline):

```bash
uv run --no-project --with pytest --with pyyaml --with pypdf --with rapidfuzz --with scipy pytest -q -p no:cacheprovider tests
```

The end-to-end scenarios in each `evals/scenarios.yaml` need a Databricks workspace, and the routing evals in `skills/forge/evals/routing.yaml` need an agent harness. The PowerShell installer's tests need PowerShell, so they're skipped where it isn't installed; GitHub Actions runs them on Linux, macOS, and Windows.

### Releasing

1. Bump `VERSION` (semver), and merge.
2. Tag and push: `git tag v$(cat VERSION) && git push origin v$(cat VERSION)`.

The workflow reruns the tests, then:
- builds the release with `tools/build_release.py`: the skills archive, the two installers and the Genie Code notebook with the version and the archive's checksum written into them, `SHA256SUMS`, and the release notes;
- publishes them as a GitHub release.

The `releases/latest/download/…` links then point at the new version. Preview a build locally with `uv run tools/build_release.py v$(cat VERSION) --out /tmp/forge-release`.

## Keeping skills current

The base skills change every few days. `jobs/forge_ucs_drift` is a Databricks job that runs daily. It:
- checks for a new [databricks-agent-skills](https://github.com/databricks/databricks-agent-skills) release;
- works out which Forge skills read the files that changed;
- asks a model whether each change matters;
- opens one pull request per affected skill. A compatible change gets a pull request that moves the skill's pin; a material or breaking one gets an edit to the skill, checked with this repository's own tests.

Nothing merges without review. It's starting in dry-run mode, which writes the pull requests' text to a volume instead of opening them. See [docs/drift-pipeline-design.md](docs/drift-pipeline-design.md).

## Roadmap

Forge is in early development. The following are planned but not yet in this repository:

- **Base skill refinement:** a Databricks workflow that refreshes the base skills monthly from release notes and documentation, with every change backed by a doc citation.
- **Certification runs:** rerunning each use-case skill's end-to-end scenarios in a workspace, so drift pull requests can say a new pin is certified, not only statically checked.
- **Use-case skill builder:** a skill that helps you combine base skills into your own use-case skill.
- **Customer and domain skills:** context about your workspace and environment that tailors a use-case skill to it.
- **Unity Catalog installer:** publishes the skills as governed Unity Catalog skills, so agents across your organization, including Genie Code, can use them.
