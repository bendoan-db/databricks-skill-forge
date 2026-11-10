# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.40"]
# ///
"""Profile ai_parse_document element confidence, and list the pages that parsed worst (shared Forge parse report).

    uv run <skill>/scripts/parse_quality.py --state <workdir>/<state file>.json

Reads <prefix>_parsed through the SQL warehouse. Element confidence ("how reliably the content was extracted")
bunches near 1.0 on born-digital files and differs by element type, so a fixed floor such as 0.8 flags nothing
there and too much on scans. Per text-bearing type (figures are left out: a logo scores low because it holds
little text, not because it parsed badly; so are page headers, footers, and numbers), this suggests a cutoff: the
--pct percentile level (nearest rank, so always an observed level). An element is flagged when it's below its
type's cutoff and below 1.0, or has no confidence. A type with fewer than --min-elements elements gets no cutoff.

It prints JSON: the profile per type, `parse_cutoffs`, the worst pages (most flagged elements, then lowest
confidence), and the parse failures. It changes nothing. Flags are triage for a person: a page to open, not an error.
"""

import argparse
import json
import math
import re
import time
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
TEXT_TYPES = ("text", "table", "title", "section_header", "caption", "footnote", "list")
LEVEL_DIGITS = 4  # levels are counted at this precision, which bounds the rows read


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/doc-index.json or extraction.json")
    p.add_argument("--pct", type=float, default=0.05, help="Percentile level to flag below (default 0.05)")
    p.add_argument("--min-elements", type=int, default=50,
                   help="Elements a type needs for a cutoff (default 50)")
    p.add_argument("--top", type=int, default=20, help="Pages to list (default 20)")
    args = p.parse_args(argv)
    if not 0 < args.pct < 1:
        p.error("--pct must be in (0, 1)")
    return args


# --- cutoffs (pure) ---------------------------------------------------------------------------------------

def level_cutoff(level_counts: dict[float, int], pct: float = 0.05) -> float | None:
    """Nearest-rank `pct` percentile of the levels in {level: count}: always an observed level."""
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


def parse_cutoffs(levels: dict[str, dict[float, int]], pct: float = 0.05, min_elements: int = 50) -> dict[str, float]:
    """Per element type with at least `min_elements` scored elements: its `pct` percentile level."""
    return {t: level_cutoff(counts, pct) for t, counts in sorted(levels.items()) if sum(counts.values()) >= min_elements}


def flag_sql(cutoffs: dict[str, float], type_col: str = "type", conf_col: str = "confidence") -> str:
    """SQL that's true for a flagged element of a text-bearing type: no confidence, or below its type's cutoff and 1.0."""
    types = ", ".join(f"'{t}'" for t in TEXT_TYPES)
    below = " ".join(f"WHEN '{t}' THEN {c!r}" for t, c in sorted(cutoffs.items()) if t in TEXT_TYPES)
    scored = f" OR ({conf_col} < 1.0 AND {conf_col} < CASE {type_col} {below} END)" if below else ""
    return f"({type_col} IN ({types}) AND ({conf_col} IS NULL{scored}))"

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
            raise SystemExit(f"{args.state.name}: {key} is missing or invalid")
    if not state.get("warehouse_id"):
        raise SystemExit(f"{args.state.name}: warehouse_id is missing (module 00)")
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient(profile=state.get("profile"))
    parsed = f"`{state['catalog']}`.`{state['schema']}`.`{state['prefix']}_parsed`"
    elements = (
        f"(SELECT p.path, e.value:type::STRING AS type, e.value:bbox[0].page_id::INT AS page_id, "
        f"e.value:confidence::DOUBLE AS confidence FROM {parsed} p, "
        f"LATERAL variant_explode(p.parsed:document.elements) AS e WHERE p.parsed IS NOT NULL)"
    )
    run = lambda sql: query(w, state["warehouse_id"], sql)  # noqa: E731

    levels: dict[str, dict[float, int]] = {}
    unscored: dict[str, int] = {}
    for kind, level, n in run(
        f"SELECT type, round(confidence, {LEVEL_DIGITS}) AS level, count(*) FROM {elements} "
        f"WHERE type IN ({', '.join(repr(t) for t in TEXT_TYPES)}) GROUP BY type, level"
    ):
        if level is None:
            unscored[kind] = int(n)
        else:
            levels.setdefault(kind, {})[float(level)] = int(n)
    cutoffs = parse_cutoffs(levels, args.pct, args.min_elements)
    pages = run(
        f"SELECT path, page_id + 1 AS page, count(*) AS elements, count_if({flag_sql(cutoffs)}) AS flagged, "
        f"min(CASE WHEN type IN ({', '.join(repr(t) for t in TEXT_TYPES)}) THEN confidence END) AS min_confidence, to_json(array_sort(collect_set(CASE WHEN {flag_sql(cutoffs)} THEN type END))) "
        f"AS flagged_types FROM {elements} GROUP BY path, page_id HAVING flagged > 0 "
        f"ORDER BY flagged DESC, min_confidence ASC NULLS FIRST LIMIT {int(args.top)}"
    )
    documents, failed = run(f"SELECT count(*), count_if(parsed IS NULL) FROM {parsed}")[0]

    print(json.dumps({
        "documents": int(documents), "parse_failed": int(failed),
        "profile": {t: {"elements": sum(levels.get(t, {}).values()) + unscored.get(t, 0), "unscored": unscored.get(t, 0),
                        "distinct_levels": len(levels.get(t, {})), "min": min(levels[t]) if t in levels else None,
                        "cutoff": cutoffs.get(t)}
                    for t in sorted(set(levels) | set(unscored))},
        "parse_cutoffs": cutoffs,
        "worst_pages": [{"path": path, "page": None if page is None else int(page), "elements": int(n), "flagged": int(flagged),
                         "min_confidence": None if low is None else float(low), "flagged_types": json.loads(types or "[]")}
                        for path, page, n, flagged, low, types in pages],
    }, indent=2))


if __name__ == "__main__":
    main()
