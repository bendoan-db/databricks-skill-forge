# 02 · Design the modules

Turn the codebase's stages into the design of a use-case skill, and get the author's approval before anything is written.

## Inputs → outputs

- **Inputs:** `stages`, `end_results`, and `end_result` from `smith.json`; `base_skills` and `forge_use_cases` from `smith-scan.json`.
- **Outputs:** the blueprint under `skill` in `smith.json`, everything but its inputs (module 03), and `design_approved: true`.

Read [anatomy.md](../references/anatomy.md) first, and use [blueprint.md](../references/blueprint.md) for the format.

## 1. One skill, or more

- **Count the end results.** One result means one skill. Two, such as an index and an agent on it, mean one skill each, and the second `requires` the first. Build one per forge-smith run, starting with the one the other needs.
- **Check what's installed.** If a Forge use case in `forge_use_cases` already produces this result, tell the author, and propose extending that skill instead. If one produces a stage, such as the index, propose `requires` for it. Continue with a new skill only if the author still wants one.

## 2. Modules

- **00-preflight,** always. Module 03 writes its questions.
- **One module per stage with its own verifiable output.** Merge stages that can't be checked apart. Split a stage that does two unrelated things.
- **Variants become choices, not modules:** alternative paths and optional features become decision points or optional inputs.
- **The last module evaluates.** If the codebase scores its own result, port that. If it doesn't, propose a metric that shows the result works for its user, with a floor, and let the author decide. Examples: retrieval hit rate, extraction accuracy against labels, answer correctness, row counts that reconcile with the source, or latency.

For each module, record its `id` (`NN-name`), `title`, `purpose`, `produces`, `sources` (the codebase files), and `scripts` (the `scripts/<file>` its sources are ported into).

## 3. Base skills and Load lines

For each module, choose from `base_skills` in `smith-scan.json`. Only installed skills can be named.

1. **Pick each base skill** whose description covers what the module does. Start from the signals, and confirm in the code.
2. **Pick one reference file** in it, by title, for the step that needs it, and read that file to confirm it covers the step. Omit `reference` only when the step needs the skill's overview.
3. **Note where a base skill is out of date,** wrong, or missing something this use case needs, and find the doc page that shows it. Each becomes an `overrides` line and a `coverage_gaps` entry with the URL. Docs beat base skills.

00-preflight loads `databricks-core`, plus whatever its checks need (module 03).

## 4. The rest of the blueprint

- **`name`, `title`, `summary`, and `description`.** The description says what the skill builds and when to use it, in up to 1024 characters. In a Forge checkout it starts "Forge use case, normally started by the forge skill:".
- **`routed`, `owner`, `upstream`, and `source`** come from `smith.json`. `source.map` lists each source file and the module it went to, or why it wasn't ported.
- **`state_file`:** `<short-name>.json`, not one an installed Forge skill uses.
- **`catalog`:**
  - `choose_when`: what the user asks for, in their words;
  - `not_for`: nearby results, and the Forge skill that builds each;
  - `produces`;
  - `next`: Forge skills that build on this one's result.
- **`thresholds`, `decision_points`, `done_when`, `overrides`, `coverage_gaps`, `requires`.**
- **`facts`:** the platform limits the code depends on, each with the doc page that states it.

## 5. Approval

Show the author:
- the modules: `# | Module | Produces | Base skills | From` (the source files);
- the decision points, and the evaluation metric with its floor;
- what you won't port, and why (anatomy.md, "What not to port").

Wait for approval, make the changes the author asks for, and record `design_approved: true`.

## Verify

- Every stage is in a module, in a decision point, or listed as not ported.
- Every base skill named is installed, and every reference file exists in it.
- There's one evaluate module, and `thresholds` isn't empty.
- The author approved the design.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No installed base skill covers a stage | Write the step from the doc pages, with **Docs:** links, and add a `coverage_gaps` entry (`kind: missing`) |
| The source uses a deprecated product | Replace it as anatomy.md says, and tell the author |
| Two stages need the same base skill and reference | Fine: each module that needs it has its own Load line |
