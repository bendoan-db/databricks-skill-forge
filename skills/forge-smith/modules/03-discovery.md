# 03 · Discovery questions

Work out what the new skill must ask its user, and what it must check, before it builds anything. These become its module 00. The questions come from every setting and assumption in the codebase.

## Inputs → outputs

- **Inputs:** `settings`, `secrets`, `resources`, and `bundles` from `smith-scan.json`; `stages` from `smith.json`.
- **Outputs:** the blueprint's `inputs`, `permissions`, `billable`, and `guardrails`, and `questions_approved: true`.

## 1. List every setting

1. **From the scan:**
   - widgets, arguments, environment variables, Spark conf, and `{{templates}}`;
   - bundle variables and job parameters;
   - secret scopes and keys;
   - hard-coded resources: volume paths, tables, models and endpoints, warehouses, clusters, schedules, and hosts.
2. **What the code assumes without a setting:**
   - data it expects to exist: a table with certain columns, files of certain types;
   - resources it expects to exist: an endpoint, a warehouse, a Genie space, an external API;
   - features that must be enabled;
   - choices it bakes in: batch or continuous, a model, a cloud.
3. **Merge duplicates:** the same value in several files is one setting.

## 2. Classify each one

| Class | When | Becomes |
|---|---|---|
| Required question | Specific to the user, with no safe default: where their data is, which existing resource to use, a name others will rely on, or a choice that changes cost or architecture (continuous or one-off, real-time or batch) | `inputs.required`, with `ask`, `label`, and `check` |
| Optional input | There's a sensible default: a model, a size, `k`, a schedule | `inputs.optional`, with `default` |
| Derived | Computed from other answers: catalog and schema from a volume path, table prefixes, job names | `inputs.derived`, with `from` |
| Check | Something to confirm, not ask: a resource exists and the user can use it, a privilege, a feature, a quota | The `check` of the input it belongs to, or a `permissions` row |
| Secret | A credential | Never asked for or stored. The user names a secret scope and key (an input), or the CLI profile covers it |
| Dropped | The author's own workspace and names, demo-only values, or values the platform decides | Nothing; list it in the report |

**Rules for the questions:**

- **Keep it to about six required questions.** Turn the rest into defaults.
- **Each `ask` says what the value is for and its format.** Propose a name where a default name is safe, such as "propose `<prefix>_index`".
- **Pair every answer that names an existing resource with a `check`:** the command that confirms it, and what to do when it fails. When the answer can be "none, create one", check that the user can create it. If they can't, they must name an existing one before the skill continues.
- **Never ask for a token, password, or key value.**

## 3. Permissions, costs, and guardrails

- **`permissions`:** one row for each object the skill reads or creates, as On and Privilege. The skill reports what's missing, and never grants it.
- **`billable`:** one line for each step that costs money: AI functions over the data, endpoints (billed while they exist), apps, clusters, scheduled or continuous jobs, and model calls. Each says when it's billed.
- **`guardrails`:** use-case rules the standard guardrails don't cover. For example, "Never write to `<claims_table>`; it's the source of record."

Add base skills that the checks need to the 00-preflight module's `base_skills`, such as `databricks-unity-catalog` for effective grants.

## 4. Approval

Show the author, as module 00 will present them:
- the questions: `Requirement | Records | What to ask`;
- the optional inputs with their defaults, and the derived values;
- the checks, the permissions, and the cost list;
- what you dropped.

Wait for approval, make the changes the author asks for, and record `questions_approved: true`.

## Verify

- Every setting from step 1 is in exactly one class.
- No required input asks for a credential's value.
- Every required input that names an existing resource has a `check`.
- The author approved the questions.

## Troubleshooting

| Symptom | Fix |
|---|---|
| More than about six required questions | Turn the ones with safe defaults into optional inputs, and derive names from one `name` input |
| A setting's purpose isn't clear from the code | Ask the author, rather than guessing a default |
| The code reads a hard-coded workspace host | Drop it: the CLI profile chooses the workspace |
