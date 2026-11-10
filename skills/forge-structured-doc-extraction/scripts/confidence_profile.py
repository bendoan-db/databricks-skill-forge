# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.40"]
# ///
"""Profile extraction confidence and suggest each document type's review cutoff (forge-structured-doc-extraction module 05).

    uv run <skill>/scripts/confidence_profile.py --state <workdir>/extraction.json

ai_extract's confidence_score isn't a smooth probability: on many corpora it takes a few levels (such as 0.95, 0.98,
and 1.0) with rare low outliers, so a fixed review_threshold like 0.8 can flag almost nothing. This reads
<prefix>_fields through the SQL warehouse, counts each document type's values by state (scored, unscored: a value
without a score, absent: no value) and by confidence level, and suggests a cutoff per type: the `--pct` percentile
level (nearest rank, so always an observed level), flagged strictly below. A type with fewer than --min-scored scored
values gets the corpus-wide level, and with fewer than that in the whole corpus, no cutoff (review_threshold alone).

It prints JSON: the profile per type, the fields with the most values to review under the suggested cutoffs, and
`review_cutoffs`, for module 05 to show the user and record in extraction.json. It changes nothing itself. A cutoff
only ever raises the floor: values are reviewed below max(review_threshold, review_cutoffs[type]).
"""

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import field_threshold  # noqa: E402  (the same threshold SQL the extract task renders)

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DISCRETE_LEVELS = 20  # at most this many distinct levels: calibrate per level (evaluate.py), else in bins
LEVEL_DIGITS = 4      # levels are counted at this precision, which bounds the rows read for continuous scores


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/extraction.json")
    p.add_argument("--pct", type=float, default=0.05, help="Percentile level to flag below (default 0.05)")
    p.add_argument("--min-scored", type=int, default=200,
                   help="Scored values a type needs for a cutoff of its own (default 200)")
    p.add_argument("--top", type=int, default=20, help="Fields to list (default 20)")
    args = p.parse_args(argv)
    if not 0 < args.pct < 1:
        p.error("--pct must be in (0, 1)")
    return args


# --- cutoffs (pure) ---------------------------------------------------------------------------------------

def level_cutoff(level_counts: dict[float, int], pct: float = 0.05) -> float | None:
    """Nearest-rank `pct` percentile of discrete levels given {level: count}: always an observed level. Flag
    strictly below it, so values tied at the cutoff aren't flagged."""
    total = sum(level_counts.values())
    if total == 0:
        return None
    rank = max(1, math.ceil(round(pct * total, 9)))  # round() guards float noise such as 10.000000000000002
    seen = 0
    for level in sorted(level_counts):
        seen += level_counts[level]
        if seen >= rank:
            return level
    return max(level_counts)


def suggest_cutoffs(levels: dict[str, dict[float, int]], pct: float = 0.05, min_scored: int = 200) -> dict[str, dict]:
    """Per document type: {cutoff, source, scored, distinct_levels}. source is `type` (its own levels), `corpus`
    (too few scored values of its own), or `floor` (too few in the whole corpus: no cutoff)."""
    corpus: dict[float, int] = {}
    for counts in levels.values():
        for level, n in counts.items():
            corpus[level] = corpus.get(level, 0) + n
    corpus_cutoff = level_cutoff(corpus, pct) if sum(corpus.values()) >= min_scored else None
    out = {}
    for doc_type, counts in levels.items():
        scored = sum(counts.values())
        if scored >= min_scored:
            cutoff, source = level_cutoff(counts, pct), "type"
        else:
            cutoff, source = corpus_cutoff, "corpus" if corpus_cutoff is not None else "floor"
        out[doc_type] = {"cutoff": cutoff, "source": source, "scored": scored, "distinct_levels": len(counts)}
    return out


def review_cutoffs(suggested: dict[str, dict], floor: float) -> dict[str, float]:
    """The cutoffs worth recording: those that raise a type above review_threshold."""
    return {t: s["cutoff"] for t, s in sorted(suggested.items()) if s["cutoff"] is not None and s["cutoff"] > floor}

# --- I/O ------------------------------------------------------------------------------------------------

def query(w, warehouse_id: str, statement: str) -> list[list]:
    from databricks.sdk.service.sql import StatementState

    response = w.statement_execution.execute_statement(warehouse_id=warehouse_id, wait_timeout="50s", statement=statement)
    while response.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(5)
        response = w.statement_execution.get_statement(response.statement_id)
    if response.status.state != StatementState.SUCCEEDED:
        raise SystemExit(f"Query failed: {response.status.error}")
    rows = list(response.result.data_array or [])
    for chunk in range(1, response.manifest.total_chunk_count or 1):
        rows += w.statement_execution.get_statement_result_chunk_n(response.statement_id, chunk).data_array or []
    return rows


def main(argv=None) -> None:
    args = parse_args(argv)
    state = json.loads(args.state.read_text())
    for key in ("catalog", "schema", "prefix"):
        if not NAME_RE.match(str(state.get(key, ""))):
            raise SystemExit(f"extraction.json: {key} is missing or invalid")
    if not state.get("warehouse_id"):
        raise SystemExit("extraction.json: warehouse_id is missing (module 00)")
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient(profile=state.get("profile"))
    fields = f"`{state['catalog']}`.`{state['schema']}`.`{state['prefix']}_fields`"
    run = lambda sql: query(w, state["warehouse_id"], sql)  # noqa: E731

    states = run(
        f"SELECT doc_type, count_if(confidence IS NOT NULL AND value IS NOT NULL) AS scored, "
        f"count_if(confidence IS NULL AND value IS NOT NULL) AS unscored, count_if(value IS NULL) AS absent "
        f"FROM {fields} GROUP BY doc_type"
    )
    levels: dict[str, dict[float, int]] = {}
    for doc_type, level, n in run(
        f"SELECT doc_type, round(confidence, {LEVEL_DIGITS}) AS level, count(*) AS n FROM {fields} "
        f"WHERE confidence IS NOT NULL AND value IS NOT NULL GROUP BY doc_type, level"
    ):
        levels.setdefault(doc_type, {})[float(level)] = int(n)

    floor = float(state.get("review_threshold", 0.8))
    suggested = suggest_cutoffs(levels, args.pct, args.min_scored)
    cutoffs = review_cutoffs(suggested, floor)
    threshold = field_threshold({"review_threshold": floor, "review_cutoffs": cutoffs}, column="doc_type")
    top = run(
        f"SELECT doc_type, regexp_replace(field, '\\\\[[0-9]+\\\\]', '[]') AS field, count(*) AS n, "
        f"count_if(confidence < {threshold}) AS low_confidence, "
        f"count_if(confidence IS NULL AND value IS NOT NULL) AS unscored "
        f"FROM {fields} GROUP BY 1, 2 HAVING low_confidence + unscored > 0 "
        f"ORDER BY low_confidence + unscored DESC LIMIT {int(args.top)}"
    )

    profile = {}
    for doc_type, scored, unscored, absent in states:
        counts = levels.get(doc_type, {})
        s = suggested.get(doc_type, {"cutoff": None, "source": "floor", "distinct_levels": 0})
        profile[doc_type] = {
            "scored": int(scored), "unscored": int(unscored), "absent": int(absent),
            "levels": [[level, counts[level]] for level in sorted(counts)][:DISCRETE_LEVELS],
            "distinct_levels": s["distinct_levels"],
            "discrete": s["distinct_levels"] <= DISCRETE_LEVELS,
            "suggested_cutoff": s["cutoff"], "cutoff_source": s["source"],
            "review_below": max(floor, s["cutoff"] or 0.0),
        }
    print(json.dumps({
        "review_threshold": floor,
        "profile": profile,
        "top_fields": [{"doc_type": t, "field": f, "values": int(n), "low_confidence": int(low), "unscored": int(u)}
                       for t, f, n, low, u in top],
        "review_cutoffs": cutoffs,
    }, indent=2))


if __name__ == "__main__":
    main()
