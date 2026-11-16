# forge-aig-evaluate-guardrails design notes

Maintainer notes for `skills/forge-aig-evaluate-guardrails`, which measures Unity AI Gateway guardrails, built-in or custom, on a labeled safety dataset. It was built from [uaig-guardrail-evals](https://github.com/bendoan-db/uaig-guardrail-evals) at `827c9e5`, owner direction 2026-11-02:
- evaluate the out-of-the-box guardrails;
- the user's dataset if they have one, asked in preflight;
- otherwise `nvidia/Aegis-AI-Content-Safety-Dataset-2.0` and `lmsys/toxic-chat`.

It was the first skill of a planned guardrail project. Setting up guardrails came next, as `forge-aig-create-model-service`, and since 0.2.0 this skill builds on it (see Building on forge-aig-create-model-service). This file isn't published.

It was named `forge-evaluate-guardrails` until 2026-11-12, when the owner renamed it to share the `forge-aig-` prefix with `forge-aig-create-model-service`. It wasn't in any release under the old name, so the installers' legacy-skill lists don't include it.

## Where the source went

| Source | Went to |
|---|---|
| `setup/01_load_data.ipynb`, `guardrail_scripts/dataset.py` (six bundled CSVs, label review, tune/test split) | `scripts/load_prompts.py`: the default datasets from Hugging Face at pinned revisions, or the user's table or file, normalized into `<prefix>_prompts`. No bundled data, label review, or split |
| `01_create_model_service.ipynb`, `guardrail_scripts/model_service.py` (plan/apply: routing, policies, inference tables, an evaluator per judge) | 0.1.0: `scripts/guardrail_service.py`, plan, apply, and a two-prompt check, for the three built-ins only. Since 0.2.0, `forge-aig-create-model-service` creates the services, and `scripts/check_services.py` only reads them and probes them |
| `guardrail_scripts/gateway.py` (the gateway call, fail closed, retries) | `gateway_call` in `scripts/run_eval.py`, and a copy in `check_services.py` for `--probe` (scripts can't import each other) |
| `02_eval_guardrail.ipynb`, `guardrail_scripts/evaluate.py`, `metrics.py` (runs, resume, results, metrics, MLflow) | `scripts/run_eval.py`, a job notebook; `scripts/evaluate.py` reads a run's metrics and gates it |
| `config.yaml` | `guardrail-eval.json` (the state file) and the rendered `src/eval_config.json` |
| `guardrails/sql/`, `guardrails/prompts/`, `guardrail_scripts/judge_dev.py` (the custom SQL guardrail and LLM judges) | Not ported. Since 0.2.0, custom `llm_judge` and `sql` policies are defined in `forge-aig-create-model-service`'s `service-policies.yaml` (ported from unity-ai-gateway-utilities) and evaluated like the built-ins |
| `guardrail_scripts/verdicts.py` (views over evaluator inference tables and `system.ai_gateway.usage`) | Not ported: the evaluation reads every verdict from the response's `databricks_service_policy` |

## Building on forge-aig-create-model-service

Owner direction, 2026-11-12: the evaluation uses `forge-aig-create-model-service` whenever it needs model services, with four decisions:
1. **Replace the create path.** `guardrail_service.py`'s plan and apply are gone, and it became `check_services.py`, which only reads. Both skills attached policies through the same undocumented `config.service_policies` PATCH, so a change there is now fixed once, in `deploy_services.py`. The cost: the default run gains the model services skill's YAML step, privilege check, plan, and go-ahead.
2. **Evaluate a copy only.** The evaluation never sends prompts to a service other people use: a service policy applies to every caller during the Beta, and a service's inference table keeps every prompt. Its services are named `<prefix>_...` (`render.py` refuses others), aren't in `permissions.yaml` (`render.py`), and nobody else holds a privilege on them (`check_services.py`; an inherited grant on the schema or catalog is only a warning, since nobody is told the service exists). To measure a production service, module 00 has the model services skill create a copy with its destinations and policies.
3. **Compare now.** One run evaluates several services on the same prompts: `eval_config.json` lists `services`, `<prefix>_results` already had `model_service`, `<prefix>_metrics` gained it (`ensure_tables` adds the column to a table from 0.1.0), `evaluate.py` compares services and gates each, and MLflow gets one run per service, tagged `eval_run_id` and `model_service`, so the metric names stay the same. Measuring each guardrail on its own, which 0.1.0 did with one run per guardrail, is now one run on three services.
4. **Leave log-mode policies out.** A policy in `mode: log` (`dry_run`) leaves no verdict in the reply, so `render.py` and `check_services.py` refuse it. A Sensitive Data Detection policy that redacts (`action: transform`) is refused for the same reason.

**The contract.** `requires` names `forge-aig-create-model-service` 0.1.1, like `forge-doc-qa-agent` and the index skill.
- **Reading, not writing.** The evaluation reads `aig-model-services.json` (`parts`, `config_dir`, `completed`) and the YAML files at render time, and never writes them. `render.py` resolves the services and their policies into `eval_config.json`, which `check_services.py` and `run_eval.py` read, so there's one resolver.
- **The entries.** Module 00, step 7 holds the YAML entries the evaluation passes for the default setup. `tests/test_aig_evaluate_guardrails_tools.py` parses them, runs them through `deploy_services.py`'s checks and `build_policy`, and checks that what it would send is what `check_services.py` accepts.
- **The evaluation's own definitions.** The built-ins get definitions of their own (`eval_sensitive_data`, `eval_unsafe_content`, `eval_jailbreak`), since a definition applies to every service that lists it and the template's own (`jailbreak_detection`, `pii_redaction`) may be the user's production settings. `eval_sensitive_data` blocks all 15 categories, as 0.1.0's policy did.
- **No inference table, no rate limits.** The results table is the evaluation's record, and the template's default of 60 requests a minute per user would throttle a run at 8 in flight.
- **The base model.** `base_model_service` (a model service whose routing 0.1.0 copied) became `base_model`, a Unity Catalog model, since the model services skill's destinations are models.
- **Its catalog `next`.** The model services skill's catalog entry now offers the evaluation, so `forge` offers it after a setup. The router hands "set up guardrails and measure them" to the evaluation, which runs the other skill itself.

## Running in Genie Code in a notebook

Owner direction, 2026-11-12: this skill and `forge-aig-create-model-service` run in Genie Code in a notebook as well as locally, with four decisions:
1. **Genie Code writes the bundle and creates the job from it.** `render.py` writes the same bundle in both places, and in a notebook `job.py` creates a job from it that runs its tasks in order. The bundle is what's left for the user: they can edit it and run it again by hand (the job's page), or with Genie Code (deploy, then run).
2. **The working directory is the user's.** In a notebook, `<workdir>` is the folder of the notebook Genie Code is open in, from the notebook's path: serverless doesn't guarantee the working directory ([workspace files](https://docs.databricks.com/aws/en/files/workspace-interact)).
3. **These two skills only.** The router, forge-smith's scaffold and `check_skill.py`, and the other use cases are unchanged. The repo README says which skills run in a notebook.
4. **The runtime is recorded once, in the state file.** Module 00 records `runtime` (`local` or `workspace`) and passes it to the model services skill, which records the same; `render.py` fails when the two state files disagree. Scripts read it rather than detecting where they run.

**How it works:**
- **Signing in.** `profile` is needed only locally. Every script builds its client with `workspace_client(state)`: the profile locally, `WorkspaceClient()` in a notebook. The SDK's notebook sign-in reads the notebook's REPL context, which a subprocess doesn't have, so in a notebook every script runs in the notebook's own process, through `forge_run` (`runpy.run_path` with `run_name="__main__"`, defined from SKILL.md). For the same reason the model services skill's `evaluate.py` runs its plan scripts with `runpy`, not `subprocess`. `tests/test_genie_code_runtime.py` checks that no script starts a subprocess, that the two helpers are copies, and that the `%pip` line installs what the scripts declare.
- **One set of modules.** The modules keep the local commands, and SKILL.md, In a notebook, maps each to its notebook equivalent (REST through `w.api_client.do`, uploads in Catalog Explorer). Only the job steps have their own notebook lines, since nothing local matches `job.py`.
- **Deploying without the CLI.** Bundles can be deployed from the workspace UI, but only from a Git folder ([bundles in the workspace](https://docs.databricks.com/aws/en/dev-tools/bundles/workspace)), and the user's folder may not be one. So `job.py deploy` does what `bundle deploy` does for this bundle: it imports `src/` into the folder the CLI would use (`/Workspace/Users/<user>/.bundle/<name>/default/files`), the notebooks through the import API with format `SOURCE` (a file written through `/Workspace` stays a file, which a notebook task can't run), and creates the job from `databricks.yml` with the CLI's references resolved. It refuses what only the CLI deploys (other task types, notebooks outside `src/`, `permissions`, other `${...}`), so an edit is never silently dropped.
- **One job.** `job.py` finds its job by name and a `forge_bundle` tag, and updates it on the next deploy. It refuses a local runtime, so a folder never gets a CLI job and a `job.py` job. A job of the same name it didn't create is left alone, with a warning. To manage the job with the CLI later, `databricks bundle deployment bind guardrail_eval <job_id>` adopts it.
- **Runs.** `job.py run` checks the job's `forge_digest` tag against the bundle, so a run after an edit stops until it's deployed. A full run is started without waiting, and `status` reads it later. Each notebook ends with `dbutils.notebook.exit(<its JSON summary>)`, so `status` (and `bundle run` locally) shows the summary as the task's output.
- **The bundle's target.** Locally it names the CLI profile. In a notebook, `render.py` drops the `# >>> profile` block, so the bundle deploys to whichever workspace it's in.

## What changed, and why

- **Built-ins, or the user's own policies, on services of the evaluation's own.** By default, the three built-ins that judge requests are attached: Sensitive Data Detection (rank 1), Unsafe Content (2), and Jailbreak (3). The source ranked them 2-4 after its SQL screen. The fourth built-in, Hallucination, is left out: it judges responses only, against grounding that a prompt dataset doesn't have.
- **Evaluators:**
  - **Model choice:** `gemma-3-12b` for Unsafe Content and `gpt-oss-20b` for Jailbreak, the source's tested choices. The source found `claude-sonnet-5` returned empty verdicts as the Jailbreak evaluator. The docs say the policy form preselects a default without naming it, so this is a `facts` entry of kind `heuristic`, and an input.
  - **No inference tables:** there's no evaluator per policy and no inference table. The source added them to audit LLM-judge verdicts. Scoring needs only each response's `databricks_service_policy`, which names the blocking policy and its reason.
- **Datasets:**
  - **Pinned:** each default file is pinned to a commit and a sha256, so a result can be reproduced and a changed file is caught.
  - **Cleaning:** Aegis 2.0 prompts that read `REDACTED` are dropped, since their text isn't distributed (the dataset card). Duplicate prompts become one row, and one whose copies disagree on the label is dropped.
  - **Labels:** Aegis's `Needs Caution` prompts are labeled `unsafe` by the dataset and kept so. Their category shows separately in the report.
  - **No egress:** a file can be staged in the volume by hand when serverless can't reach huggingface.co.
- **Licenses:** ToxicChat is CC BY-NC 4.0, so preflight states both licenses and the user approves each. A dataset they can't use is left out.
- **Scoring:**
  - **No precision restriction:** precision, the false-positive rate, and F1 are computed wherever defined, not only for the source's `pii` and `toxicity` groups, since both default datasets mix both kinds of prompt.
  - **Categories:** Aegis's violated categories and ToxicChat's `toxicity` and `jailbreaking` flags.
- **The gate is the run's completeness, not the guardrails' quality.** `THRESHOLDS = {"scored_rate": 0.98}`: the share of prompts with a verdict. How good the guardrails must be is the user's call, so `targets` are reported as met or missed and never fail the run.
- **Job code is notebooks** (`load_prompts`, `run_eval`), deployed as notebook tasks in a bundle, with `disable_auto_optimization` on `run_eval`. The source found that serverless retries a failed task even with `max_retries: 0`, and a retried new run starts a second run. `run_eval` installs `mlflow-skinny` in its first cell, as the source does, since the serverless default environment has no MLflow.
- **Docs over the source.** The docs (2026-10-29) say a workspace admin enables the Unity Gateway Service Policies preview, where the source said an account admin did. They also say policy changes take a minute or two to propagate; the skill waits `settle_minutes` (5, the source's value) before a run.

## Not yet verified

Nothing has run in a workspace. The tests cover the following, offline:
- normalizing the datasets;
- prompt ids and the sample;
- the gateway call against a fake client: blocks, fail closed, retries, and errors;
- scoring, and the MLflow metric names;
- module 00's entries, through the model services skill's checks and `build_policy`;
- `check_services.py` against a fake client: policies, blocking, who else holds privileges, and the probe outcomes;
- rendering the services from the model services skill's files, and refusing ones that aren't the evaluation's own;
- several services in one run: sending, scoring, MLflow params, and the comparison report;
- the report and the gate;
- rendering, and keeping edited copies;
- in a notebook: the target without a profile, the two state files' runtime, `job.py`'s job settings, imports, create and update, the deploy check before a run, its tasks and parameters, and reading a run, against a fake client; `forge_run` on the scripts.

These are assumptions:
1. **Attaching by API.** Attaching policies with `PATCH /api/2.1/unity-catalog/model-services/<name>?update_mask=config.service_policies` isn't documented. It worked for the source on 2026-10-01. The docs show the UI; `forge-aig-create-model-service`'s module 03 falls back to it.
2. **Defaults the source set explicitly.** The built-ins' option names and values: `phases`, `dry_run`, `max_turns`, `categories`, `action`, and `model_service`, every value a string. It's also unknown whether `model_service` can be left out to get the default evaluator.
3. **The blocked response.** It carries `finish_reason: content_filter` and `databricks_service_policy.{name, phase, reason, action}`. A policy that fails to evaluate answers HTTP 500 "Input policy '<name>' failed to evaluate" (the source's observation; the docs say only that it denies).
4. **The CLI.** `databricks ai-gateway get-model-service` still leaves out `config.service_policies` on the current CLI (seen on 1.18.0).
5. **Custom policies' blocks.** That a block by a custom `llm_judge` or `sql` policy comes back with the same `databricks_service_policy` shape as a built-in's. The job counts a block only when it does, or when `finish_reason` is `content_filter`.
6. **Redaction.** That a redacting Sensitive Data Detection policy leaves no marker in the reply. The skill refuses such policies rather than relying on one.
7. **Who can call a service.** That `GET /api/2.1/unity-catalog/effective-permissions/model_service/<name>` lists every principal's grants, inherited ones with `inherited_from_name`, for the service's owner.
8. **Serverless notebook tasks:**
   - egress to huggingface.co;
   - `%pip install mlflow-skinny` and `restartPython()`;
   - the job parameters `mode` and `run_id` reaching the notebook as widgets;
   - `disable_auto_optimization` as a bundle task field.
9. **The evaluator defaults:** that `gemma-3-12b` and `gpt-oss-20b` are what the policy form preselects today.
10. **The warehouse reads.** `evaluate.py` reads the metrics through the Statement Execution API with named parameters.

The scenarios in `evals/scenarios.yaml` exercise each of these.

In a notebook, these are assumptions too:
- **Signing in.** `WorkspaceClient()` in the notebook's process reaches every API the scripts call: model services, effective permissions, SCIM `Me`, statement execution, the gateway's chat completions, Jobs, and Workspace import. A subprocess wouldn't, which is why `forge_run` uses `runpy`.
- **Genie Code waits for the go-aheads.** Its tool-approval classifier approves actions by default; the skill's cost and apply confirmations depend on Genie Code stopping for the user's answer.
- **Files.** The `install_genie_code` notebook writes the skill's `.py` files through `/Workspace`, so `render.py` reads `load_prompts.py` and `run_eval.py` as files; and the import API puts a `SOURCE` notebook under `.bundle/` without its extension, as the CLI's deploy does.
- **Jobs API.** `jobs/list` filters on the exact name, `run-now` takes `only`, and `runs/get-output` returns `notebook_output.result` for a notebook task.
- **Serverless.** The notebook's environment has Python 3.11 or later, and `notebookPath()` works there.

