# 01 · Scan

Inventory the codebase, then read it until you can name each stage of the solution, before designing anything.

## Inputs → outputs

- **Inputs:** `codebase` and `out` from `smith.json`.
- **Outputs:** `<workdir>/smith-scan.json`, and `stages` in `smith.json`.

## 1. Run the scan

```bash
uv run <smith>/scripts/scan.py <codebase> --out <workdir>/smith-scan.json --skills-dir <out>
```

It prints a summary. Tell the author now about any hard-coded tokens or deprecated products it reports. The JSON holds:

| Key | What |
|---|---|
| `files`, `docs`, `numbered_files` | Every text file with its kind (notebook, python, sql, bundle, config, app, docs, …), README headings, and files with numbered names, which often give the order |
| `bundles` | Each bundle's jobs, with their tasks in dependency order, plus its variables and targets |
| `signals` | Base skills the code appears to use, with evidence locations, and whether each is installed |
| `settings`, `secrets` | Widgets, arguments, environment variables, Spark conf, `{{templates}}`, and secret scopes and keys |
| `resources` | Hard-coded volume paths, tables, models and endpoints, warehouses, clusters, schedules, and workspace hosts |
| `base_skills` | Each installed base skill: version, description, and reference files with their titles |
| `forge_use_cases` | Each installed Forge use case's catalog entry |

## 2. Read the code

Read in this order, and stop once you can describe each stage:

1. The README and other docs.
2. The bundle jobs. Their task order is usually the pipeline order.
3. The numbered notebooks, then the files the job tasks run, then what those import.

Skip vendored, generated, and test files. Signals are pattern matches: confirm each in the code before relying on it.

## 3. Map the stages

Write `stages` in `smith.json`, in order. Each stage has:
- `name`;
- `does`: one sentence;
- `files`;
- `reads` and `creates`: tables, volumes, indexes, endpoints, apps, jobs, and files;
- `features`: the platform features it uses.

Also record:
- `support`: shared helpers and config;
- `ignored`: scratch work, demos, and tests, each with a reason;
- `end_results`: what the codebase produces for its users. More than one, such as an index and an agent, is a decision for module 02.

## Verify

- `smith-scan.json` exists, and you've told the author about any hard-coded tokens.
- Every notebook, python, sql, bundle, and app file is in a stage, in `support`, or in `ignored`.
- Each stage names what it creates.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `isn't a folder` | Clone or export the code first (module 00, step 2) |
| `over_file_limit` in `skipped` | The repo is larger than the solution: scan the subfolder that holds it, and record that as `codebase` |
| A `.dbc` archive, or notebooks show up as binary | Export them as source files (`databricks workspace export-dir`), then scan the export |
| No signals | The code may use only Spark and SQL. The stages still come from reading it |
| `base_skills` is empty | `--skills-dir` doesn't hold the base skills; pass `<out>` |
