# forge-evaluate-guardrails design notes

Maintainer notes for `skills/forge-evaluate-guardrails`, which measures Unity AI Gateway's built-in guardrails on a labeled safety dataset. It was built from [uaig-guardrail-evals](https://github.com/bendoan-db/uaig-guardrail-evals) at `827c9e5`, owner direction 2026-11-02:
- evaluate the out-of-the-box guardrails;
- the user's dataset if they have one, asked in preflight;
- otherwise `nvidia/Aegis-AI-Content-Safety-Dataset-2.0` and `lmsys/toxic-chat`.

It's the first skill of a planned guardrail project; setting up custom guardrails comes later. This file isn't published.

## Where the source went

| Source | Went to |
|---|---|
| `setup/01_load_data.ipynb`, `guardrail_scripts/dataset.py` (six bundled CSVs, label review, tune/test split) | `scripts/load_prompts.py`: the default datasets from Hugging Face at pinned revisions, or the user's table or file, normalized into `<prefix>_prompts`. No bundled data, label review, or split |
| `01_create_model_service.ipynb`, `guardrail_scripts/model_service.py` (plan/apply: routing, policies, inference tables, an evaluator per judge) | `scripts/guardrail_service.py`: plan, apply, and a two-prompt check, for the three built-ins only |
| `guardrail_scripts/gateway.py` (the gateway call, fail closed, retries) | `gateway_call` in `scripts/run_eval.py`, and a copy in `guardrail_service.py` for `--check` (scripts can't import each other) |
| `02_eval_guardrail.ipynb`, `guardrail_scripts/evaluate.py`, `metrics.py` (runs, resume, results, metrics, MLflow) | `scripts/run_eval.py`, a job notebook; `scripts/evaluate.py` reads a run's metrics and gates it |
| `config.yaml` | `guardrail-eval.json` (the state file) and the rendered `src/eval_config.json` |
| `guardrails/sql/`, `guardrails/prompts/`, `guardrail_scripts/judge_dev.py` (the custom SQL guardrail and LLM judges) | Not ported: out of scope for the built-ins |
| `guardrail_scripts/verdicts.py` (views over evaluator inference tables and `system.ai_gateway.usage`) | Not ported: the evaluation reads every verdict from the response's `databricks_service_policy` |

## What changed, and why

- **Built-ins only, on a service of the evaluation's own.** The three built-ins that judge requests are attached: Sensitive Data Detection (rank 1), Unsafe Content (2), and Jailbreak (3). The source ranked them 2-4 after its SQL screen. The fourth built-in, Hallucination, is left out: it judges responses only, against grounding that a prompt dataset doesn't have.
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
- the policy entries, plan and apply against a fake client, and the check outcomes;
- the report and the gate;
- rendering, and keeping edited copies.

These are assumptions:
1. **Attaching by API.** Attaching the built-ins with `PATCH /api/2.1/unity-catalog/model-services/<name>?update_mask=config.service_policies` isn't documented. It worked for the source on 2026-10-01. The docs show the UI; module 02 falls back to it.
2. **Defaults the source set explicitly.** The built-ins' option names and values: `phases`, `dry_run`, `max_turns`, `categories`, `action`, and `model_service`, every value a string. It's also unknown whether `model_service` can be left out to get the default evaluator.
3. **The blocked response.** It carries `finish_reason: content_filter` and `databricks_service_policy.{name, phase, reason, action}`. A policy that fails to evaluate answers HTTP 500 "Input policy '<name>' failed to evaluate" (the source's observation; the docs say only that it denies).
4. **The CLI.** `databricks ai-gateway get-model-service` still leaves out `config.service_policies` on the current CLI (seen on 1.18.0).
5. **Serverless notebook tasks:**
   - egress to huggingface.co;
   - `%pip install mlflow-skinny` and `restartPython()`;
   - the job parameters `mode` and `run_id` reaching the notebook as widgets;
   - `disable_auto_optimization` as a bundle task field.
6. **The evaluator defaults:** that `gemma-3-12b` and `gpt-oss-20b` are what the policy form preselects today.
7. **The warehouse reads.** `evaluate.py` reads the metrics through the Statement Execution API with named parameters.

The scenarios in `evals/scenarios.yaml` exercise each of these.
