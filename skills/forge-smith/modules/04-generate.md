# 04 · Generate

Write the skill folder from the blueprint, then fill in what only you can write: the steps, the ported scripts, and the diagram.

## Inputs → outputs

- **Inputs:** the approved blueprint in `smith.json` (`design_approved` and `questions_approved` are both true), and the codebase.
- **Outputs:** `<out>/<name>/`, with no `smith:` markers left.

## 1. Scaffold

```bash
uv run <smith>/scripts/scaffold.py --state <workdir>/smith.json --skills-dir <out>
```

It writes `<out>/<name>/` and reports how many `smith:` markers are left to fill. It fills in everything the blueprint settles:
- the frontmatter, and the Inputs, Guardrails, Workflow, and Decision points sections;
- module 00, whole: its questions, checks, permissions, cost list, and state file;
- each module's heading and Load lines;
- `ucs.yaml`, with the installed base-skill versions;
- the thresholds, in `evaluate.py` and `scenarios.yaml`;
- a copy of `check_base_skills.py`.

If it lists blueprint problems instead, fix the blueprint (modules 02 and 03), and run it again.

## 2. Port the scripts

For each stub in `scripts/`, port the source files its module came from (the stub names them):

- **Read every setting from the state file,** `--state <workdir>/<state_file>`, by the input names from module 03. Hard-code nothing.
- **Keep the source's logic,** and fix what anatomy.md lists under "Improve on the source". Note each change for the design note (module 05).
- **Job and training code is a Databricks notebook.** Keep the stub's cells, its `build_parser()` flags, and its last cell (anatomy.md, Scripts). Put packages in its `%pip` cell. A job becomes a bundle template the skill renders from the state file, with a `notebook_task` per notebook, on serverless compute.
- **App code stays a Python module** (`app_scripts`), and local tools declare their dependencies in the PEP 723 header.
- **Don't run any of it.** It runs when the skill is certified.

Write `measure()` in `scripts/evaluate.py` the same way.

## 3. Write the modules

In each module, replace the markers:

- **Steps:** numbered, with commands that use `--profile <profile>` and `<key>` values from the state file.
- **Load lines:** move each one to the step that needs it, keeping the same set of base skills.
- **Verify:** observable checks, such as row counts, a query, or an endpoint that answers.
- **Troubleshooting:** a row for each error the source handles or documents.

Keep each module short. Don't restate what a base skill covers: the Load line is enough.

## 4. The rest

- **SKILL.md:** the Reference architecture diagram, plus anything missing from Done when.
- **`evals/scenarios.yaml`:** one scenario, from the source's example data or a public dataset, with values for the required inputs, its checks, and the teardown list.
- **Other markers:** run `grep -rn "smith:" <out>/<name>` and fill in whatever it finds.

## Verify

- `grep -rn "smith:" <out>/<name>` finds nothing.
- Each Python script parses: `python -B -c "import ast, sys; ast.parse(open(sys.argv[1]).read())" <file>`.
- No script or module contains a token, a workspace URL, or the author's own catalog, schema, or paths.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `already exists` | The folder is from an earlier run. Edit it by hand, or, with the author's confirmation naming it, move it aside and scaffold again |
| `isn't installed in` | The blueprint names a base skill that isn't in `<out>`. Pick an installed one, or install it |
| `has no references/…` | The reference file isn't in that base skill's installed version. Pick one that is (`base_skills` in `smith-scan.json`) |
| A source file is too big to port as one script | Split it by stage, one script per module, and share helpers through one file |
