"""The pipeline's tables: Delta in the workspace, JSON Lines files in a local development run.

Every table is append-only and keyed by run_id, so each run's decisions stay inspectable. Nested values are
JSON strings, to keep the schemas flat.
"""

import datetime
import json
from pathlib import Path

TABLES = {
    "drift_runs": "run_id STRING, at TIMESTAMP, status STRING, dry_run BOOLEAN, forge_sha STRING, latest_tag STRING, "
                  "message STRING",
    "upstream_releases": "tag STRING, published_at STRING, html_url STRING, first_seen_run STRING",
    "upstream_skills": "tag STRING, skill STRING, dir STRING, experimental BOOLEAN, version STRING, description STRING, "
                       "files_json STRING",
    "ucs_dependencies": "run_id STRING, forge_skill STRING, module STRING, base_skill STRING, base_version STRING, "
                        "cited_file STRING",
    "ucs_impacts": "run_id STRING, forge_skill STRING, from_tag STRING, to_tag STRING, base_skill STRING, kind STRING, "
                   "severity STRING, paths_json STRING, modules_json STRING, detail STRING",
    "ucs_drift_assessments": "run_id STRING, forge_skill STRING, base_skill STRING, from_tag STRING, to_tag STRING, "
                             "materiality STRING, summary STRING, assessment_json STRING, error STRING, model STRING, "
                             "prompt_version STRING",
    "ucs_drift_proposals": "run_id STRING, forge_skill STRING, action STRING, from_tag STRING, to_tag STRING, "
                           "materiality STRING, new_version STRING, valid BOOLEAN, attempts INT, summary STRING, "
                           "failures_json STRING, flags_json STRING, artifact_dir STRING, model STRING, prompt_version STRING",
    "ucs_drift_prs": "run_id STRING, forge_skill STRING, status STRING, branch STRING, pr_number INT, pr_url STRING, "
                     "commit_sha STRING, message STRING",
}


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class LocalStore:
    def __init__(self, root: Path):
        self.root = root / "tables"
        self.root.mkdir(parents=True, exist_ok=True)

    def append(self, table: str, rows: list[dict]) -> None:
        if rows:
            with (self.root / f"{table}.jsonl").open("a") as f:
                for row in rows:
                    f.write(json.dumps(row, default=str) + "\n")

    def delete(self, table: str, run_id: str) -> None:
        path = self.root / f"{table}.jsonl"
        if path.is_file():
            kept = [line for line in path.read_text().splitlines() if line.strip() and json.loads(line).get("run_id") != run_id]
            path.write_text("".join(line + "\n" for line in kept))

    def rows(self, table: str, **where) -> list[dict]:
        path = self.root / f"{table}.jsonl"
        if not path.is_file():
            return []
        found = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [r for r in found if all(r.get(k) == v for k, v in where.items())]


class DeltaStore:
    def __init__(self, spark, settings):
        self.spark, self.settings = spark, settings
        for name, ddl in TABLES.items():
            spark.sql(f"CREATE TABLE IF NOT EXISTS {settings.table(name)} ({ddl}) "
                      "COMMENT 'forge_ucs_drift (jobs/forge_ucs_drift in databricks-skill-forge)'")

    def append(self, table: str, rows: list[dict]) -> None:
        if rows:
            columns = [c.split()[0] for c in TABLES[table].split(", ")]
            self.spark.createDataFrame([tuple(r.get(c) for c in columns) for r in rows], TABLES[table]) \
                .write.mode("append").saveAsTable(self.settings.table(table))

    def delete(self, table: str, run_id: str) -> None:
        self.spark.sql(f"DELETE FROM {self.settings.table(table)} WHERE run_id = :run_id", args={"run_id": run_id})

    def rows(self, table: str, **where) -> list[dict]:
        df = self.spark.table(self.settings.table(table))
        for column, value in where.items():
            df = df.where(df[column] == value)
        return [r.asDict() for r in df.collect()]
