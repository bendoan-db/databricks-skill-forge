# Databricks notebook source
# MAGIC %md
# MAGIC # Run the guardrail evaluation
# MAGIC
# MAGIC The `run_eval` task of the forge-evaluate-guardrails evaluation job: it sends each prompt through the evaluation's
# MAGIC model service, records whether a guardrail blocked it and which one, and scores the run into the metrics table and
# MAGIC MLflow. The job passes its settings as the widgets at the top (`mode` and `run_id` come from the job's parameters);
# MAGIC to run it by hand, fill them in. To keep a change you make here, export this notebook into the bundle's `src/`
# MAGIC folder before the next deploy, which otherwise replaces it (module 01).

# COMMAND ----------

# MAGIC %pip install -q "mlflow-skinny>=3"

# COMMAND ----------

"""Send the prompts through the guarded model service and score the run (forge-evaluate-guardrails module 03).

Second task of the evaluation job, after load_prompts.py. It's a Databricks notebook: it runs as a serverless notebook
task whose parameters arrive as widgets, or locally through Databricks Connect, with the same settings as flags:

    uv run --with "databricks-connect>=17" --with "mlflow-skinny>=3" python run_eval.py --catalog main \
        --schema guardrail_eval --prefix guardrail_eval --config eval_config.json --mode sample

Before sending anything it checks that the model service has exactly the configured guardrails, and waits until
settle_minutes have passed since it last changed (policy changes take a few minutes to take effect). A run sends
`sample_size` prompts per dataset and label (mode `sample`) or all of them (`full`), with `concurrency` calls in flight,
and appends one row per prompt to <prefix>_results in batches. A run is resumable: give its run_id again, and only the
prompts it hasn't sent are sent. It then replaces the run's rows in <prefix>_metrics, and logs the run to MLflow.

A blocked prompt is a positive. A guardrail that fails to evaluate denies the request (the gateway fails closed), so
that counts as a block by that guardrail; a rate-limited call is retried instead, and a call that still fails is an
ERROR, which is counted but not scored.
"""

if "dbutils" in globals():  # in a notebook, load the mlflow %pip installed
    dbutils.library.restartPython()  # noqa: F821

# COMMAND ----------

import argparse  # noqa: E402 - after the restart cell, as a notebook needs
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PREFIX_RE = re.compile(r"^[a-z0-9_]+$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
RUN_ID_RE = re.compile(r"^eval-(sample|full)-\d{12}$")
MODES = ("sample", "full")
API = "/api/2.1/unity-catalog/model-services"
GATEWAY_PATH = "/ai-gateway/mlflow/v1/chat/completions"
MAX_CHARS = 12000  # prompts are cut to this before they're sent
BATCH = 200  # results are appended in batches of this many rows
BLOCK, ALLOW = "BLOCK", "ALLOW"
RESULT_CLASSES = ("TRUE_POSITIVE", "FALSE_NEGATIVE", "FALSE_POSITIVE", "TRUE_NEGATIVE")
RESULTS_DDL = ("prompt_id STRING, run_id STRING, run_ts TIMESTAMP, mode STRING, model_service STRING, dataset STRING, "
               "categories ARRAY<STRING>, expected STRING, blocked BOOLEAN, blocking_policy STRING, phase STRING, "
               "reason STRING, finish_reason STRING, http_status INT, latency_ms DOUBLE, prompt_tokens INT, "
               "total_tokens INT, result_class STRING, error STRING, response STRING")
METRICS_DDL = ("run_id STRING, run_ts TIMESTAMP, mode STRING, breakdown STRING, dataset STRING, group_name STRING, "
               "n BIGINT, expected_block BIGINT, expected_allow BIGINT, blocked BIGINT, tp BIGINT, fn BIGINT, fp BIGINT, "
               "tn BIGINT, errors BIGINT, recall DOUBLE, precision DOUBLE, fp_rate DOUBLE, accuracy DOUBLE, f1 DOUBLE, "
               "latency_p50_ms DOUBLE, latency_p95_ms DOUBLE")
RESULT_COLUMNS = [c.split()[0] for c in RESULTS_DDL.split(", ")]
METRIC_COLUMNS = [c.split()[0] for c in METRICS_DDL.split(", ")]
# What scoring reads back. Not run_id: on serverless, a big result filtered on it can come back in Arrow batches that
# disagree on whether it can be null, and the read fails.
SCORED_COLUMNS = ["prompt_id", "dataset", "categories", "expected", "blocked", "blocking_policy", "result_class",
                  "latency_ms"]
COUNTS = ("n", "expected_block", "expected_allow", "blocked", "tp", "fn", "fp", "tn", "errors")
RATES = ("recall", "precision", "fp_rate", "accuracy", "f1")
CONFIG_KEYS = ("model_service", "policies", "concurrency", "max_tokens", "sample_size", "settle_minutes", "experiment")

# COMMAND ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])  # a notebook has no __doc__
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--prefix", required=True, help="Table prefix: reads <prefix>_prompts, writes _results and _metrics")
    p.add_argument("--config", default="", help="eval_config.json, which render.py writes (default: next to this file)")
    p.add_argument("--mode", choices=MODES, default="sample",
                   help="sample: sample_size prompts per dataset and label; full: every prompt")
    p.add_argument("--run-id", default="", help="An earlier run's id, to resume it; empty for a new run")
    return p


def notebook_argv(parser: argparse.ArgumentParser) -> list[str]:
    """The notebook's widgets as command-line flags. A job's notebook task passes each setting as a widget named
    like its flag (--volume-path is volume_path); a required one left empty is reported as missing."""
    argv = []
    for action in parser._actions:  # noqa: SLF001 - argparse has no public list of options
        if not action.option_strings or action.dest == "help":
            continue
        dbutils.widgets.text(action.dest, "" if action.default is None else str(action.default))  # noqa: F821
        value = dbutils.widgets.get(action.dest)  # noqa: F821 - dbutils exists in Databricks notebooks
        if value or action.default is not None:
            argv += [action.option_strings[0], value]
    return argv


def script_dir() -> Path:
    """This file's folder: next to the script, or a notebook's own workspace folder, its working directory."""
    return Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()


def load_config(path: Path) -> dict:
    """eval_config.json without render.py's checksum key, checked for what this task reads."""
    if not path.is_file():
        raise SystemExit(f"{path} not found: render.py writes it into the bundle's src/ (module 01)")
    config = {k: v for k, v in json.loads(path.read_text()).items() if k != "_rendered_by"}
    errors = [f"{key} is missing" for key in CONFIG_KEYS if config.get(key) in (None, "", [])]
    if config.get("model_service") and not SERVICE_RE.match(str(config["model_service"])):
        errors.append(f"model_service={config['model_service']!r}: use <catalog>.<schema>.<service>")
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    return config


def new_run(mode: str, run_id: str, now: datetime) -> dict:
    """The run's id and mode. A new run is eval-<mode>-<UTC minute>; a given run_id is resumed, with the mode its id
    names (so resuming a full run with the job's default mode still sends every prompt)."""
    if run_id:
        match = RUN_ID_RE.match(run_id)
        return {"run_id": run_id, "mode": match.group(1) if match else mode}
    return {"run_id": f"eval-{mode}-{now:%Y%m%d%H%M}", "mode": mode}


def select_sample(prompts: list[dict], size: int) -> list[dict]:
    """For each dataset and label, the `size` prompts with the smallest prompt_id: the same sample every time."""
    groups: dict[tuple, list[dict]] = {}
    for p in prompts:
        groups.setdefault((p["dataset"], p["expected"]), []).append(p)
    return [p for key in sorted(groups) for p in sorted(groups[key], key=lambda p: p["prompt_id"])[:size]]

# COMMAND ----------

TRANSIENT = {"TooManyRequests", "InternalError", "TemporarilyUnavailable", "DeadlineExceeded", "ConnectionError",
             "TimeoutError"}  # SDK error classes (and builtins) matched by name, so the SDK stays optional
STATUS = {"BadRequest": 400, "Unauthenticated": 401, "PermissionDenied": 403, "NotFound": 404, "ResourceConflict": 409,
          "TooManyRequests": 429, "InternalError": 500, "NotImplemented": 501, "TemporarilyUnavailable": 503,
          "DeadlineExceeded": 504}
FAILED = re.compile(r"(Input|Output) policy '([^']+)' failed to evaluate", re.I)
RATE_LIMITED = re.compile(r"\b429\b|rate.?limit|too many requests|REQUEST_LIMIT_EXCEEDED", re.I)


def _names(e: Exception) -> set[str]:
    return {cls.__name__ for cls in type(e).__mro__}


def gateway_call(w, model_service: str, prompt: str, max_tokens: int, attempts: int = 6, sleep=time.sleep) -> dict:
    """Send one prompt to the model service through Unity AI Gateway, and read whether a guardrail blocked it.

    A block comes back as HTTP 200 with finish_reason content_filter and a top-level databricks_service_policy
    {name, action, phase, reason} (phase pre_call: the request was blocked; post_call: the reply). A guardrail that
    can't reach a verdict fails closed: HTTP 500 "Input policy '<name>' failed to evaluate", recorded as a block by it
    and not retried. A failure caused by a rate limit is load from this evaluation, so it's retried like any other
    transient error. Returns {blocked, policy, phase, reason, action, finish_reason, content, prompt_tokens,
    total_tokens, http, latency_ms, error}."""
    body = {"model": model_service, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
    out = {"blocked": False, "policy": None, "phase": None, "reason": None, "action": None, "finish_reason": None,
           "content": None, "prompt_tokens": None, "total_tokens": None, "http": None, "latency_ms": None, "error": None}
    started = time.time()
    response = None
    for attempt in range(attempts):
        try:
            response = w.api_client.do("POST", GATEWAY_PATH, body=body)
            out["http"], out["error"] = 200, None
            break
        except Exception as e:  # noqa: BLE001 - every failure is recorded on the result
            text = str(e)
            out["http"] = getattr(e, "status_code", None) or STATUS.get(type(e).__name__, -1)
            out["error"] = f"{type(e).__name__}: {text}"[:800]
            failed = FAILED.search(text)
            if failed and not RATE_LIMITED.search(text):  # fail closed: the gateway denied the request
                out.update(blocked=True, policy=failed.group(2), action="deny",
                           phase="pre_call" if failed.group(1).lower() == "input" else "post_call",
                           reason=f"evaluation failed (fail closed): {text[:300]}")
                break
            if not (_names(e) & TRANSIENT) or attempt == attempts - 1:
                break
            sleep(min(2 ** attempt, 30))
    out["latency_ms"] = round((time.time() - started) * 1000, 1)
    if response is None:
        return out
    choice = (response.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    policy = response.get("databricks_service_policy") or {}
    usage = response.get("usage") or {}
    out.update(finish_reason=choice.get("finish_reason"), content=content, policy=policy.get("name"),
               phase=policy.get("phase"), reason=policy.get("reason"), action=policy.get("action"),
               prompt_tokens=usage.get("prompt_tokens"), total_tokens=usage.get("total_tokens"))
    out["blocked"] = out["finish_reason"] == "content_filter" or str(out["action"] or "").lower() == "deny"
    return out


def result_class(expected: str, blocked: bool, errored: bool) -> str:
    """ERROR when the call failed without a verdict; else the prompt's place in the confusion matrix (blocked = positive)."""
    if errored and not blocked:
        return "ERROR"
    if expected == BLOCK:
        return "TRUE_POSITIVE" if blocked else "FALSE_NEGATIVE"
    return "FALSE_POSITIVE" if blocked else "TRUE_NEGATIVE"


def record(prompt: dict, out: dict, run: dict, model_service: str, now: datetime) -> dict:
    """One results-table row for a prompt and its gateway outcome."""
    errored = out["error"] is not None and not out["blocked"]
    return {
        "prompt_id": prompt["prompt_id"], "run_id": run["run_id"], "run_ts": now, "mode": run["mode"],
        "model_service": model_service, "dataset": prompt["dataset"], "categories": list(prompt.get("categories") or []),
        "expected": prompt["expected"], "blocked": bool(out["blocked"]), "blocking_policy": out["policy"],
        "phase": out["phase"], "reason": None if out["reason"] is None else str(out["reason"])[:2000],
        "finish_reason": out["finish_reason"], "http_status": out["http"], "latency_ms": out["latency_ms"],
        "prompt_tokens": out["prompt_tokens"], "total_tokens": out["total_tokens"],
        "result_class": result_class(prompt["expected"], bool(out["blocked"]), errored), "error": out["error"],
        "response": None if out["content"] is None else str(out["content"])[:2000],
    }

# COMMAND ----------

def _ratio(a: int, b: int) -> float | None:
    return a / b if b else None


def _percentile(values: list[float], q: float) -> float | None:
    """The q-th percentile (0-100), interpolated between the nearest ranks."""
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q / 100
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (k - low), 1)


def _score(rows: list[dict], latency: bool) -> dict:
    classes = [r["result_class"] for r in rows]
    tp, fn, fp, tn = (classes.count(c) for c in RESULT_CLASSES)
    recall, precision = _ratio(tp, tp + fn), _ratio(tp, tp + fp)
    defined = recall is not None and precision is not None and precision + recall
    f1 = 2 * precision * recall / (precision + recall) if defined else None
    times = [r["latency_ms"] for r in rows if r["result_class"] != "ERROR" and r.get("latency_ms") is not None]
    return {"n": len(rows), "expected_block": tp + fn, "expected_allow": fp + tn, "blocked": tp + fp,
            "tp": tp, "fn": fn, "fp": fp, "tn": tn, "errors": classes.count("ERROR"),
            "recall": recall, "precision": precision, "fp_rate": _ratio(fp, fp + tn),
            "accuracy": _ratio(tp + tn, tp + fn + fp + tn), "f1": f1,
            "latency_p50_ms": _percentile(times, 50) if latency else None,
            "latency_p95_ms": _percentile(times, 95) if latency else None}


def compute(rows: list[dict]) -> list[dict]:
    """The metrics rows for one run's results: overall, per dataset, per dataset and category (over the prompts that
    carry the category), and per blocking guardrail (its blocks and how many were false positives). ERROR rows count
    in n and errors, not in the confusion counts; a rate with no denominator is None."""
    out = [{"breakdown": "overall", "dataset": "all", "group_name": "all", **_score(rows, True)}]
    datasets = sorted({r["dataset"] for r in rows})
    for dataset in datasets:
        out.append({"breakdown": "dataset", "dataset": dataset, "group_name": dataset,
                    **_score([r for r in rows if r["dataset"] == dataset], True)})
    for dataset in datasets:
        in_dataset = [r for r in rows if r["dataset"] == dataset]
        for category in sorted({c for r in in_dataset for c in r.get("categories") or []}):
            out.append({"breakdown": "category", "dataset": dataset, "group_name": category,
                        **_score([r for r in in_dataset if category in (r.get("categories") or [])], False)})
    blocks: dict[str, list[dict]] = {}
    for r in rows:
        if r["blocked"] and r["result_class"] != "ERROR":
            blocks.setdefault(r.get("blocking_policy") or "(unnamed)", []).append(r)
    for policy, group in sorted(blocks.items(), key=lambda item: (-len(item[1]), item[0])):
        tp = sum(r["result_class"] == "TRUE_POSITIVE" for r in group)
        fp = sum(r["result_class"] == "FALSE_POSITIVE" for r in group)
        out.append({"breakdown": "policy", "dataset": "all", "group_name": policy, **dict.fromkeys(COUNTS),
                    **dict.fromkeys(RATES), "latency_p50_ms": None, "latency_p95_ms": None,
                    "n": len(group), "blocked": len(group), "tp": tp, "fp": fp})
    return out


def _slug(value) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", str(value)).strip("_").lower()


def mlflow_metrics(rows: list[dict]) -> dict:
    """{name: value} for MLflow: overall.<rate> and dataset.<dataset>.<rate>, with their counts and latency;
    category.<dataset>.<category>.recall; policy.<guardrail>.blocks and .fp. Undefined values are left out."""
    out = {}
    for r in rows:
        if r["breakdown"] == "policy":
            out[f"policy.{_slug(r['group_name'])}.blocks"] = r["blocked"]
            out[f"policy.{_slug(r['group_name'])}.fp"] = r["fp"]
        elif r["breakdown"] == "category":
            if r["recall"] is not None:
                out[f"category.{_slug(r['dataset'])}.{_slug(r['group_name'])}.recall"] = r["recall"]
        else:
            prefix = "overall" if r["breakdown"] == "overall" else f"dataset.{_slug(r['group_name'])}"
            for key in (*RATES, "tp", "fn", "fp", "tn", "errors", "latency_p50_ms", "latency_p95_ms"):
                if r[key] is not None:
                    out[f"{prefix}.{key}"] = r[key]
    return out

# COMMAND ----------

def get_spark():
    if "DATABRICKS_RUNTIME_VERSION" in os.environ:
        from pyspark.sql import SparkSession

        return SparkSession.builder.getOrCreate()
    from databricks.connect import DatabricksSession

    return DatabricksSession.builder.serverless().getOrCreate()


def live_policies(service: dict) -> list[dict]:
    return [p for p in ((service.get("config") or {}).get("service_policies") or []) if not p.get("is_deleted")]


def readiness(service: dict, policies: list[str], settle_minutes: float, now: datetime) -> tuple[str | None, float]:
    """(a problem, or None; seconds to wait). The model service must have exactly the configured guardrails, and
    settle_minutes must have passed since it last changed."""
    live = sorted(p["name"] for p in live_policies(service))
    if set(live) != set(policies):
        return (f"{service.get('name') or 'the model service'} has the guardrails {live}, not {sorted(policies)}: "
                "run guardrail_service.py --apply (module 02)", 0.0)
    updated = service.get("update_time")
    if isinstance(updated, (int, float)):
        changed = datetime.fromtimestamp(updated / 1000, timezone.utc)
    elif updated:
        changed = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
    else:
        return None, 0.0
    return None, max(0.0, settle_minutes * 60 - (now - changed).total_seconds())


def wait_until_ready(w, model_service: str, policies: list[str], settle_minutes: float) -> dict:
    """The model service as read, once it matches the config and has settled."""
    try:
        service = w.api_client.do("GET", f"{API}/{model_service}")
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "NotFound":
            raise SystemExit(f"{model_service} doesn't exist: run guardrail_service.py --apply (module 02)") from e
        raise
    problem, wait = readiness(service, policies, settle_minutes, datetime.now(timezone.utc))
    if problem:
        raise SystemExit(problem)
    if wait:
        print(f"{model_service} changed recently; waiting {wait / 60:.1f} min for its guardrails to take effect")
        time.sleep(wait)
    return service


def ensure_tables(spark, results: str, metrics: str) -> None:
    spark.sql(f"CREATE TABLE IF NOT EXISTS {results} ({RESULTS_DDL})")
    spark.sql(f"CREATE TABLE IF NOT EXISTS {metrics} ({METRICS_DDL})")


def done_ids(spark, results: str, run_id: str) -> set[str]:
    rows = spark.sql(f"SELECT prompt_id FROM {results} WHERE run_id = :run_id", args={"run_id": run_id}).collect()
    return {r.prompt_id for r in rows}


def append(spark, table: str, ddl: str, columns: list[str], rows: list[dict]) -> None:
    spark.createDataFrame([tuple(r[c] for c in columns) for r in rows], ddl).write.mode("append").saveAsTable(table)


def send_all(w, run: dict, prompts: list[dict], model_service: str, concurrency: int, max_tokens: int, write) -> int:
    """Send every prompt with `concurrency` calls in flight, passing each batch of result rows to write(rows) as it
    fills. Returns how many were sent."""
    buffer, sent, started = [], 0, time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(gateway_call, w, model_service, p["prompt"][:MAX_CHARS], max_tokens): p for p in prompts}
        for future in as_completed(futures):
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            buffer.append(record(futures[future], future.result(), run, model_service, now))
            sent += 1
            if len(buffer) >= BATCH:
                write(buffer)
                buffer = []
            if sent % 500 == 0:
                print(f"{sent}/{len(prompts)} sent, {(time.time() - started) / 60:.1f} min", flush=True)
    if buffer:
        write(buffer)
    return sent


def run_results(spark, results: str, run_id: str) -> list[dict]:
    """The run's rows, with the columns scoring reads."""
    rows = spark.sql(f"SELECT {', '.join(SCORED_COLUMNS)} FROM {results} WHERE run_id = :run_id",
                     args={"run_id": run_id}).collect()
    return [r.asDict(recursive=True) for r in rows]


def save_metrics(spark, metrics: str, run: dict, rows: list[dict]) -> None:
    """Replace the run's rows in the metrics table."""
    spark.sql(f"DELETE FROM {metrics} WHERE run_id = :run_id", args={"run_id": run["run_id"]})
    ts = datetime.now(timezone.utc).replace(tzinfo=None)
    append(spark, metrics, METRICS_DDL, METRIC_COLUMNS,
           [{"run_id": run["run_id"], "run_ts": ts, "mode": run["mode"], **r} for r in rows])


def log_mlflow(w, config: dict, run: dict, rows: list[dict], service: dict, prompts: int) -> tuple[str, str]:
    """Log the run to the MLflow experiment (a workspace path, or a name under your home folder). A rerun of the same
    run_id updates its MLflow run; params are logged only on its first pass, since MLflow can't change them."""
    import mlflow

    experiment = config["experiment"]
    if not experiment.startswith("/"):
        experiment = f"/Users/{w.current_user.me().user_name}/{experiment}"
    mlflow.set_experiment(experiment)
    found = mlflow.search_runs(filter_string=f"tags.eval_run_id = '{run['run_id']}'", output_format="list")
    with mlflow.start_run(run_id=found[0].info.run_id if found else None, run_name=run["run_id"]) as active:
        mlflow.set_tags({"eval_run_id": run["run_id"], "mode": run["mode"], "model_service": config["model_service"]})
        if not found:
            params = {"model_service": config["model_service"], "mode": run["mode"], "prompts": prompts,
                      "concurrency": config["concurrency"], "max_tokens": config["max_tokens"]}
            for p in live_policies(service):
                options = {k: v for k, v in (p.get("options") or {}).items() if k != "categories"}
                summary = f"rank {p.get('rank')} | {p.get('handler')} | {json.dumps(options, sort_keys=True)}"
                params[f"policy.{p['name']}"] = summary[:500]  # MLflow caps a param's length
            mlflow.log_params(params)
        mlflow.log_metrics(mlflow_metrics(rows))
        mlflow.log_dict({"metrics": rows}, "metrics.json")
        mlflow.log_dict({"service_policies": live_policies(service)}, "service_policies.json")
    return experiment, active.info.run_id

# COMMAND ----------

def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    for name in (args.catalog, args.schema):
        if not NAME_RE.match(name):
            raise SystemExit(f"Invalid name {name!r}: use letters, digits, '_' or '-'")
    if not PREFIX_RE.match(args.prefix):
        raise SystemExit(f"Invalid prefix {args.prefix!r}: use lowercase letters, digits, and '_'")
    config = load_config(Path(args.config) if args.config else script_dir() / "eval_config.json")
    table = lambda suffix: f"`{args.catalog}`.`{args.schema}`.`{args.prefix}_{suffix}`"  # noqa: E731
    prompts_table, results, metrics = table("prompts"), table("results"), table("metrics")

    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    spark = get_spark()
    ensure_tables(spark, results, metrics)
    service = wait_until_ready(w, config["model_service"], config["policies"], float(config["settle_minutes"]))
    run = new_run(args.mode, args.run_id, datetime.now(timezone.utc))

    prompts = [r.asDict(recursive=True) for r in spark.sql(
        f"SELECT prompt_id, dataset, prompt, expected, categories FROM {prompts_table}").collect()]
    if not prompts:
        raise SystemExit(f"{prompts_table} is empty: run the load_prompts task first (module 01)")
    chosen = prompts if run["mode"] == "full" else select_sample(prompts, int(config["sample_size"]))
    done = done_ids(spark, results, run["run_id"])
    todo = [p for p in chosen if p["prompt_id"] not in done]
    print(f"{run['run_id']}: {len(chosen)} prompts, {len(done)} already sent, {len(todo)} to send")
    sent = send_all(w, run, todo, config["model_service"], int(config["concurrency"]), int(config["max_tokens"]),
                    lambda rows: append(spark, results, RESULTS_DDL, RESULT_COLUMNS, rows))

    rows = compute(run_results(spark, results, run["run_id"]))
    save_metrics(spark, metrics, run, rows)
    experiment, mlflow_run = log_mlflow(w, config, run, rows, service, len(chosen))
    overall = rows[0]
    print(json.dumps({
        "run_id": run["run_id"], "mode": run["mode"], "prompts": len(chosen), "sent": sent,
        **{k: overall[k] for k in (*RATES, "errors", "latency_p50_ms", "latency_p95_ms")},
        "experiment": experiment, "mlflow_run_id": mlflow_run,
    }))

# COMMAND ----------

# As a job's notebook task, the settings arrive as widgets; run as a script, as command-line flags.
if __name__ == "__main__":
    main(notebook_argv(build_parser()) if "dbutils" in globals() else None)
