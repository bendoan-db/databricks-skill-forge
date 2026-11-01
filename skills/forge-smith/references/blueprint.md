# The blueprint

The design of the new skill, kept under `skill` in `<workdir>/smith.json`. Module 02 writes most of it and module 03 writes `inputs`, `permissions`, `billable`, and `guardrails`. Then `scaffold.py` turns it into the skill folder, and checks it first: every base skill must be installed, and every reference file must exist.

```json
{
  "skill": {
    "name": "forge-claims-triage",
    "title": "Insurance claims triage",
    "summary": "Scores incoming claims for fraud risk and routes each one to a queue. It reads new claims from a table as they land, ...",
    "description": "Forge use case, normally started by the forge skill: scores insurance claims for fraud risk with ... Use when forge hands off to it, or when the user names this skill.",
    "routed": true,
    "owner": "@claims-team",
    "upstream": "v0.2.35",
    "state_file": "claims.json",
    "source": {
      "repo": "https://github.com/acme/claims-accelerator",
      "commit": "60ab408",
      "map": [
        {"from": "notebooks/01_ingest.py", "to": "01-ingest"},
        {"from": "notebooks/02_features.sql", "to": "02-score"},
        {"from": "notebooks/99_demo_data.py", "to": "not ported: demo data"}
      ]
    },
    "catalog": {
      "title": "Claims triage",
      "choose_when": "fraud scores and queues for incoming insurance claims",
      "not_for": "fields extracted from claim documents (forge-structured-doc-extraction)",
      "produces": "a scoring job, a scored claims table, and an accuracy evaluation in MLflow",
      "next": []
    },
    "inputs": {
      "required": [
        {
          "name": "claims_table",
          "label": "Where new claims land",
          "ask": "The three-part name of the table new claims arrive in, such as main.claims.raw",
          "check": "`databricks tables get <claims_table> --profile <profile>` succeeds, and it has the columns in module 01's table",
          "notes": "Read only; the skill never writes to it"
        },
        {
          "name": "continuous",
          "label": "Continuous scoring",
          "ask": "Score claims as they arrive (a streaming job, billed while it runs), or on a schedule?"
        }
      ],
      "optional": [
        {"name": "target_schema", "default": "the claims table's catalog and schema", "notes": "Where the scored table goes"},
        {"name": "model", "default": "Claude Sonnet 5.5: `system.ai.claude-sonnet-5-5`", "notes": "Writes the triage notes"}
      ],
      "derived": [
        {"name": "prefix", "from": "the claims table's name, snake_case"}
      ]
    },
    "permissions": [
      {"on": "claims table", "privilege": "`SELECT`"},
      {"on": "target schema", "privilege": "`CREATE TABLE`, `USE SCHEMA`"}
    ],
    "billable": [
      "**Scoring job:** a serverless job; with continuous scoring, billed while it runs",
      "**Triage notes:** one model call per claim"
    ],
    "guardrails": [],
    "modules": [
      {"id": "00-preflight",
       "base_skills": [{"name": "databricks-core", "uses": ["profile selection", "auth"]},
                       {"name": "databricks-unity-catalog", "reference": "references/1-access-control.md", "uses": ["effective grants"]}]},
      {"id": "01-ingest", "title": "Ingest", "purpose": "Read new claims incrementally into a staging table.",
       "produces": "`<prefix>_staged`", "sources": ["notebooks/01_ingest.py"], "scripts": ["scripts/ingest.py"],
       "base_skills": [{"name": "databricks-spark-structured-streaming", "reference": "references/trigger-and-cost-optimization.md", "uses": ["availableNow trigger"]}]},
      {"id": "02-evaluate", "title": "Evaluate", "purpose": "Score a labeled sample and compare with the thresholds.",
       "produces": "an MLflow run, pass/fail against the thresholds", "sources": ["notebooks/05_eval.py"],
       "base_skills": [{"name": "databricks-mlflow-evaluation", "uses": ["experiment logging"]}]}
    ],
    "thresholds": {"precision_at_review": 0.8},
    "decision_points": [
      {"decision": "Scoring trigger", "default": "Asked in module 00", "switch_when": "Claims arrive in large daily batches: schedule it instead"}
    ],
    "done_when": ["`<prefix>_scored` has a row for every claim in the claims table"],
    "overrides": [],
    "coverage_gaps": [],
    "facts": [],
    "requires": []
  }
}
```

| Field | Notes |
|---|---|
| `name` | Lowercase letters, digits, and hyphens. In a Forge checkout, `forge-<what it produces>` |
| `summary`, `description` | The SKILL.md introduction, and the frontmatter description (up to 1024 characters) |
| `routed` | `true` in a Forge checkout: the skill says `forge` starts it, and its scenarios start through `forge` |
| `upstream` | The databricks-agent-skills tag the base skills come from (module 00) |
| `state_file` | The skill's state file in `<workdir>`; not one an installed Forge skill uses |
| `source.map` | Each source file, and the module it went to, or why it wasn't ported. Module 05 copies it into the design note |
| `inputs.required` | The discovery questions (module 03). `ask` is required. `label` names it in module 00's table. `check` is how module 00 confirms the answer. `notes` fills the Inputs table |
| `inputs.optional` | `default` and `notes`. `profile` is added when missing |
| `inputs.derived` | Values module 00 computes and shows back to the user |
| `permissions`, `billable` | Module 00's permissions table and cost list. `billable` also fills the confirmation guardrail |
| `modules` | `00-preflight` first (it must load `databricks-core`), and one module whose id ends in `-evaluate`. Each other module needs `title` and `produces`. `sources` are codebase files, and `scripts` are the files the module ports them into (`scripts/<file>`; stubs are written). `base_skills` names installed skills; `reference` is one file in the skill, or omitted to mean its SKILL.md |
| `thresholds` | `{metric: floor}`. Written into `scripts/evaluate.py` and `evals/scenarios.yaml` |
| `decision_points`, `done_when`, `overrides`, `guardrails` | Rows and lines for those SKILL.md sections. An override has `topic` and `guidance`, plus a `coverage_gaps` entry with its evidence |
| `coverage_gaps`, `facts`, `requires` | Copied into `ucs.yaml` as they are (see anatomy.md) |
