# Databricks notebook source
# MAGIC %md
# MAGIC # Sync the search index
# MAGIC
# MAGIC The `sync_index` task of the forge-unstructured-doc-extraction index job: it creates the AI Search index over
# MAGIC the chunks table, or syncs it, and waits until every chunk is indexed. The job passes its settings as the
# MAGIC widgets at the top; to run it by hand, fill them in. To keep a change you make here, export this notebook into
# MAGIC the bundle's `src/` folder before the next deploy, which otherwise replaces it (module 01).

# COMMAND ----------

# MAGIC %pip install -q "databricks-sdk>=0.124.0"

# COMMAND ----------

"""Create or sync the AI Search Delta Sync index over the chunks table (forge-unstructured-doc-extraction module 02).

Last task of the index job, after prep_chunks.py. It's a Databricks notebook: it runs as a serverless notebook
task whose parameters arrive as widgets, or locally, with the same settings as flags. The endpoint must
already exist; module 02 creates it after the user confirms. The task waits until the index serves every row
in the chunks table, so the job only succeeds once retrieval sees the new documents.
"""

if "dbutils" in globals():  # in a notebook, load the SDK version %pip installed rather than the preinstalled one
    dbutils.library.restartPython()  # noqa: F821

# COMMAND ----------

import argparse  # noqa: E402 - after the restart cell, as a notebook needs
import os  # noqa: E402
import time  # noqa: E402

from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.errors import DatabricksError, NotFound  # noqa: E402
from databricks.sdk.service.vectorsearch import (  # noqa: E402
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    EndpointStatusState,
    PipelineType,
    VectorIndexType,
)

BASE_SYNC_COLUMNS = ["chunk_id", "source_uri", "file_name", "chunk_position", "page_numbers"]
POLL_SECONDS = 30

# COMMAND ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])  # a notebook has no __doc__
    p.add_argument("--chunks-table", required=True, help="<catalog>.<schema>.<prefix>_chunks")
    p.add_argument("--index-name", required=True, help="<catalog>.<schema>.<index_name>")
    p.add_argument("--endpoint", required=True)
    p.add_argument("--embedding-model", required=True, help="Embedding serving endpoint name")
    p.add_argument("--metadata-columns", default="", help="Comma-separated metadata columns to sync")
    p.add_argument("--timeout-minutes", type=int, default=90)
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

# COMMAND ----------

def source_row_count(table: str) -> int:
    if "DATABRICKS_RUNTIME_VERSION" in os.environ:
        from pyspark.sql import SparkSession

        spark = SparkSession.builder.getOrCreate()
    else:
        from databricks.connect import DatabricksSession

        spark = DatabricksSession.builder.serverless().getOrCreate()
    return spark.table(table).count()


def wait_for_endpoint(w: WorkspaceClient, name: str, deadline: float) -> None:
    while True:
        try:
            status = w.vector_search_endpoints.get_endpoint(name).endpoint_status
        except NotFound:
            raise SystemExit(f"AI Search endpoint {name!r} not found. Create it in module 02 first.") from None
        state = status.state if status else None
        if state == EndpointStatusState.ONLINE:
            return
        if state in (EndpointStatusState.OFFLINE, EndpointStatusState.RED_STATE, EndpointStatusState.DELETED):
            raise SystemExit(f"Endpoint {name!r} is {state.value}: {status.message}")
        if time.time() > deadline:
            raise TimeoutError(f"Endpoint {name!r} not ONLINE (state={state})")
        print(f"endpoint {name}: {state}; waiting")
        time.sleep(POLL_SECONDS)


def source_table(index) -> str | None:
    spec = getattr(index, "delta_sync_index_spec", None)
    return getattr(spec, "source_table", None)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    deadline = time.time() + args.timeout_minutes * 60
    w = WorkspaceClient()
    wait_for_endpoint(w, args.endpoint, deadline)

    metadata = [c.strip() for c in args.metadata_columns.split(",") if c.strip()]
    try:
        existing = w.vector_search_indexes.get_index(args.index_name)
        if (source := source_table(existing)) and source.lower() != args.chunks_table.lower():
            raise SystemExit(
                f"{args.index_name} already exists and is built from {source}, not {args.chunks_table}. "
                "Choose another index_name (module 00, step 6) rather than syncing someone else's index."
            )
        try:
            w.vector_search_indexes.sync_index(args.index_name)
            print(f"sync triggered for {args.index_name}")
        except DatabricksError as e:
            # A sync still running from an earlier trigger is fine: wait for it instead of failing.
            if not any(word in str(e).lower() for word in ("already", "in progress", "running")):
                raise
            print(f"sync not triggered ({e}); waiting for the running one")
    except NotFound:
        w.vector_search_indexes.create_index(
            name=args.index_name,
            endpoint_name=args.endpoint,
            primary_key="chunk_id",
            index_type=VectorIndexType.DELTA_SYNC,
            delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
                source_table=args.chunks_table,
                pipeline_type=PipelineType.TRIGGERED,
                embedding_source_columns=[
                    EmbeddingSourceColumn(
                        name="chunk_to_embed", embedding_model_endpoint_name=args.embedding_model
                    )
                ],
                # chunk_to_embed (embedding source) and chunk_id (primary key) are always synced.
                columns_to_sync=BASE_SYNC_COLUMNS + metadata,
            ),
        )
        print(f"created {args.index_name}; the initial sync starts automatically")

    expected = source_row_count(args.chunks_table)
    while True:
        status = w.vector_search_indexes.get_index(args.index_name).status
        indexed = status.indexed_row_count if status else None
        message = (status.message or "") if status else ""
        if "FAILED" in message.upper():
            raise SystemExit(f"Index sync failed: {message}")
        if status and status.ready and indexed == expected:
            print(f"{args.index_name} ready: {indexed} rows")
            return
        if time.time() > deadline:
            raise TimeoutError(f"{args.index_name}: indexed {indexed} of {expected} rows ({message})")
        print(f"indexed {indexed}/{expected} rows; ready={status.ready if status else None}")
        time.sleep(POLL_SECONDS)

# COMMAND ----------

# As a job's notebook task, the settings arrive as widgets; run as a script, as command-line flags.
if __name__ == "__main__":
    main(notebook_argv(build_parser()) if "dbutils" in globals() else None)
