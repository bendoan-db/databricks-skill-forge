# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.68"]
# ///
"""Report a forge-evaluate-guardrails evaluation run, and check it's complete enough to trust (module 03).

    uv run <skill>/scripts/evaluate.py --state <workdir>/guardrail-eval.json [--run-id <id>]

Reads the run's rows in <catalog>.<schema>.<prefix>_metrics through the SQL warehouse in the state file (by default
the latest run with mode `full`), and prints:
- overall and per dataset: recall, precision, false-positive rate, F1, accuracy, errors, and latency;
- per policy: the blocks it made, and how many were false positives;
- the 10 categories with the lowest recall;
- each of the user's `targets`, met or missed.

THRESHOLDS gates the run itself, not the guardrails: the share of prompts that got a verdict from the gateway
(`scored_rate`; an ERROR row, a call that failed after its retries, has none). Below the floor the metrics aren't
trustworthy, and the script exits with status 1. A missed target is reported, not a failure: the targets are the user's
bar for the guardrails, and this skill measures them rather than tuning them.
"""

import argparse
import json
import sys
from pathlib import Path

THRESHOLDS = {"scored_rate": 0.98}
RATES = ("recall", "precision", "fp_rate", "f1", "accuracy")
CEILINGS = {"fp_rate"}  # a target for these is a maximum; for the other rates, a minimum
COUNTS = ("n", "expected_block", "expected_allow", "blocked", "tp", "fn", "fp", "tn", "errors")
NUMERIC = (*COUNTS, *RATES, "latency_p50_ms", "latency_p95_ms")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/guardrail-eval.json")
    p.add_argument("--run-id", default="", help="The run to report (default: the latest full run)")
    return p.parse_args()


def overall(rows: list[dict]) -> dict | None:
    return next((r for r in rows if r.get("breakdown") == "overall"), None)


def scored_rate(row: dict) -> float | None:
    n = row.get("n") or 0
    scored = sum(row.get(k) or 0 for k in ("tp", "fn", "fp", "tn"))
    return scored / n if n else None


def threshold_failures(overall_row: dict | None) -> list[str]:
    """Each THRESHOLDS floor the run's overall row misses."""
    if not overall_row:
        return ["the run has no overall metrics row"]
    rate = scored_rate(overall_row)
    floor = THRESHOLDS["scored_rate"]
    if rate is None or rate < floor:
        n, errors = overall_row.get("n") or 0, overall_row.get("errors") or 0
        shown = "none" if rate is None else f"{rate:.3f}"
        return [f"scored_rate={shown} < {floor}: {errors} of {n} prompts errored, so the metrics aren't trustworthy. "
                "Resume the run with its run_id to resend them"]
    return []


def target_results(rows: list[dict], targets: dict | None) -> list[dict]:
    """Each target against the run's overall value: {metric, target, kind, value, met}."""
    row = overall(rows) or {}
    results = []
    for metric, target in (targets or {}).items():
        value = row.get(metric)
        kind = "ceiling" if metric in CEILINGS else "floor"
        met = value is not None and (value <= target if kind == "ceiling" else value >= target)
        results.append({"metric": metric, "target": target, "kind": kind, "value": value, "met": met})
    return results


def pct(value) -> str:
    return "-" if value is None else f"{100 * value:.1f}%"


def ms(value) -> str:
    return "-" if value is None else f"{value / 1000:.1f} s"


def report(rows: list[dict], targets: dict | None) -> str:
    """The run's report as text."""
    lines = []
    head = overall(rows)
    if head:
        lines.append(f"Run {head.get('run_id')} ({head.get('mode')}): {head.get('n')} prompts, {head.get('errors')} errors, "
                     f"scored rate {pct(scored_rate(head))}")
    lines += ["", "| Dataset | Prompts | Recall | Precision | FP rate | F1 | Accuracy | Errors | p50 | p95 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in [head] * bool(head) + [r for r in rows if r.get("breakdown") == "dataset"]:
        name = "all" if r.get("breakdown") == "overall" else r.get("group_name")
        lines.append(f"| {name} | {r.get('n')} | {pct(r.get('recall'))} | {pct(r.get('precision'))} | "
                     f"{pct(r.get('fp_rate'))} | {pct(r.get('f1'))} | {pct(r.get('accuracy'))} | {r.get('errors')} | "
                     f"{ms(r.get('latency_p50_ms'))} | {ms(r.get('latency_p95_ms'))} |")
    policies = sorted((r for r in rows if r.get("breakdown") == "policy"), key=lambda r: -(r.get("blocked") or 0))
    if policies:
        lines += ["", "| Policy | Blocks | False positives |", "|---|---:|---:|"]
        lines += [f"| {r.get('group_name')} | {r.get('blocked')} | {r.get('fp')} |" for r in policies]
    categories = sorted((r for r in rows if r.get("breakdown") == "category" and r.get("recall") is not None),
                        key=lambda r: (r["recall"], -(r.get("n") or 0)))[:10]
    if categories:
        lines += ["", "Lowest recall by category:", "", "| Dataset | Category | Prompts | Recall |", "|---|---|---:|---:|"]
        lines += [f"| {r.get('dataset')} | {r.get('group_name')} | {r.get('n')} | {pct(r.get('recall'))} |" for r in categories]
    checked = target_results(rows, targets)
    if checked:
        lines += ["", "| Target | Wanted | Measured | Met |", "|---|---:|---:|---|"]
        lines += [f"| {t['metric']} | {'≤' if t['kind'] == 'ceiling' else '≥'} {pct(t['target'])} | {pct(t['value'])} | "
                  f"{'yes' if t['met'] else 'no'} |" for t in checked]
    return "\n".join(lines)


def typed(row: dict) -> dict:
    """A metrics row from the SQL warehouse (every value a string) with its numbers as numbers."""
    out = dict(row)
    for key in NUMERIC:
        if out.get(key) is not None:
            out[key] = int(out[key]) if key in COUNTS else float(out[key])
    return out


def query(w, warehouse_id: str, statement: str, parameters: dict) -> list[dict]:
    """Rows of one SQL statement, run on the warehouse, as dicts of strings."""
    from databricks.sdk.service.sql import StatementParameterListItem, StatementState

    response = w.statement_execution.execute_statement(
        statement=statement, warehouse_id=warehouse_id, wait_timeout="50s",
        parameters=[StatementParameterListItem(name=k, value=v) for k, v in parameters.items()])
    while response.status.state in (StatementState.PENDING, StatementState.RUNNING):
        response = w.statement_execution.get_statement(response.statement_id)
    if response.status.state != StatementState.SUCCEEDED:
        raise SystemExit(f"Query failed: {response.status.error.message if response.status.error else response.status.state}")
    names = [c.name for c in response.manifest.schema.columns]
    data = list((response.result and response.result.data_array) or [])
    chunk = response.result
    while chunk and chunk.next_chunk_index is not None:
        chunk = w.statement_execution.get_statement_result_chunk_n(response.statement_id, chunk.next_chunk_index)
        data += chunk.data_array or []
    return [dict(zip(names, values, strict=True)) for values in data]


def main() -> None:
    args = parse_args()
    if not args.state.is_file():
        sys.exit(f"{args.state} not found. Module 00 writes it; run preflight first.")
    state = json.loads(args.state.read_text())
    missing = [k for k in ("profile", "catalog", "schema", "prefix", "warehouse_id") if not state.get(k)]
    if missing:
        sys.exit(f"{args.state}: {missing} missing; module 00 records them")
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient(profile=state["profile"])
    table = f"`{state['catalog']}`.`{state['schema']}`.`{state['prefix']}_metrics`"
    run_id = args.run_id
    if not run_id:
        latest = query(w, state["warehouse_id"],
                       f"SELECT run_id FROM {table} WHERE mode = 'full' ORDER BY run_ts DESC LIMIT 1", {})
        if not latest:
            sys.exit(f"No full run in {table}. Run the job with mode=full (module 03), or pass --run-id.")
        run_id = latest[0]["run_id"]
    rows = [typed(r) for r in query(w, state["warehouse_id"], f"SELECT * FROM {table} WHERE run_id = :run_id",
                                    {"run_id": run_id})]
    if not rows:
        sys.exit(f"No metrics for run {run_id} in {table}.")
    print(report(rows, state.get("targets")))
    failures = threshold_failures(overall(rows))
    targets = target_results(rows, state.get("targets"))
    print(json.dumps({"run_id": run_id, "scored_rate": scored_rate(overall(rows) or {}), "thresholds_met": not failures,
                      "failures": failures, "targets_met": all(t["met"] for t in targets) if targets else None}))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
