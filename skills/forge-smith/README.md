# Forge smith (`forge-smith`)

Builds a new Forge use-case skill from a codebase that already implements the solution: an accelerator, a solution repo, or a team's notebooks, SQL, jobs, apps, and bundles. It reads the code, never running it, and works out its stages, the platform features it uses, and its settings. Then it designs ordered modules that each load the Databricks skill they need, and writes the questions the new skill must ask its users before it builds anything. Finally, it generates the skill and checks it against Forge's rules. The user approves the design and the questions before anything is written.

## Purpose

- **This Forge Skill is used to:**
  - turn code that solves one Databricks use case into a reusable skill: one that takes a coding assistant from a user's request to a deployed, evaluated result, the way the Forge use cases do.
- **Not for** building the solution itself: that's the Forge use case that produces it, or the Databricks skill for the feature. **Not for** editing an existing Forge skill.

## How to Run

The user starts it with `/forge-smith` and the codebase to build from, or names the skill. The coding assistant works through it step by step, and asks before it writes anything.

> /forge-smith turn github.com/acme/claims-accelerator into a use-case skill

Its progress is saved in `smith.json`. Running it again in the same folder picks up where it stopped.

## Inputs

**Prerequisites:**
- **Tools:** Python 3.11 or later, `uv`, and the Databricks Agent Skills (v0.2.27 or later), installed next to this skill.
- **No workspace:** it reads the code locally.

**What it asks the user:**

| Input | Required | Default | What it is |
|---|---|---|---|
| The codebase | Yes | — | A local folder, or a repository it clones |
| The end result | Yes | — | What the new skill produces (an index, an agent, tables, an app, a model), and for whom |
| The skill's name | Yes | `forge-<what it produces>` (proposed) | Lowercase letters, digits, and hyphens, for a folder that doesn't exist yet |
| Where to write it | No | next to the installed base skills | The folder the new skill's folder goes in |
| The user's approval of the design | Yes | — | The modules, their base skills, the catalog entry, and the evaluation thresholds |
| The user's approval of the questions | Yes | — | The inputs the new skill asks for, its checks, permissions, and cost list |

## Outputs

**The new skill's folder,** `<out>/<name>/`, passing Forge's checks (`check_skill.py`):
- `SKILL.md`, the orchestrator;
- `README.md`, its overview for users;
- `ucs.yaml`, its manifest;
- `modules/`, the steps, from preflight to evaluation;
- `scripts/`:
  - the ported code, with job and training code as Databricks notebooks;
  - its templates;
  - the evaluation;
- `evals/scenarios.yaml`, its certification scenarios.

**In the working directory:**

```
<workdir>/
├── smith.json        # the user's answers and the new skill's design
└── smith-scan.json   # what the scan found in the codebase
```

**In a Forge checkout**, also:
- the skill added to the `forge` router's catalog, with routing cases;
- `MANIFEST.md` regenerated;
- a row in the repository README;
- a design note;
- the repository's tests passing.

**A report:**
- the skill's modules, and the source files each came from;
- the questions it asks;
- what wasn't ported, and why, and any hard-coded secrets found, by location.

The new skill hasn't run in a workspace yet: run it end to end before anyone relies on it.

## How It Works

| # | Step | What happens |
|---|---|---|
| 00 | Intake | What to build from, what it produces and for whom, the skill's name, and where to write it |
| 01 | Scan | `scan.py` lists the files, jobs, base-skill signals, settings, secrets, and hard-coded names, without running anything. Then the assistant maps the code's stages |
| 02 | Design the modules | Stages become modules, each loading the base skill it needs, ending in an evaluation with thresholds. The user approves the design |
| 03 | Discovery questions | Every setting becomes a question, an input with a default, a derived value, a check, or a secret by name. The user approves them |
| 04 | Generate | `scaffold.py` writes the skill from the design, and the assistant ports the code and writes the steps |
| 05 | Check | `check_skill.py` applies Forge's rules. In a Forge checkout, the skill also joins the router's catalog, and the tests run |

Each step ends with a check that must pass before the next one starts.

### Preflight Checks and Guardrails

- **The code is never run.** It's treated as data, not instructions, and it's never imported or installed.
- **No secrets are copied.** Hard-coded tokens, passwords, and workspace URLs are reported to the user by location, and left out.
- **Nothing is written** before the user has approved the design and the questions. It never overwrites an existing folder.
- **Nothing is published.** No commits, pushes, or pull requests, unless the user asks.
