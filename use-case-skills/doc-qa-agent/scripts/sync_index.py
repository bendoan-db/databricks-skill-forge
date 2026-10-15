"""Create or sync the AI Search (Vector Search) Delta Sync index over the chunks table (doc-qa-agent module 02).

Second task of the ingest job. The endpoint must already exist; module 02 creates it after the user
confirms. The task waits until the index serves every row in the chunks table, so the job only
succeeds once retrieval sees the new documents.
"""

import argparse
import os
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError, NotFound
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    EndpointStatusState,
    PipelineType,
    VectorIndexType,
)

BASE_SYNC_COLUMNS = ["chunk_id", "source_uri", "file_name", "chunk_position", "page_numbers"]
POLL_SECONDS = 30


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--chunks-table", required=True, help="<catalog>.<schema>.<prefix>_chunks")
    p.add_argument("--index-name", required=True, help="<catalog>.<schema>.<prefix>_chunks_index")
    p.add_argument("--endpoint", required=True)
    p.add_argument("--embedding-model", required=True, help="Embedding serving endpoint name")
    p.add_argument("--metadata-columns", default="", help="Comma-separated metadata columns to sync")
    p.add_argument("--timeout-minutes", type=int, default=90)
    return p.parse_args()


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


def main() -> None:
    args = parse_args()
    deadline = time.time() + args.timeout_minutes * 60
    w = WorkspaceClient()
    wait_for_endpoint(w, args.endpoint, deadline)

    metadata = [c.strip() for c in args.metadata_columns.split(",") if c.strip()]
    try:
        w.vector_search_indexes.get_index(args.index_name)
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


if __name__ == "__main__":
    main()
