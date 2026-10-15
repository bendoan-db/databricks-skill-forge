"""Document search tool for the doc-qa agent (module 03), rendered to agent/retrieval.py.

`render.py agent` fills the constants from doc-qa.json, so change them there. The tool is built
lazily on first use, which keeps importing the agent side-effect-free, as the template requires.
It authenticates as whatever identity ``workspace_client()`` resolves: your CLI profile locally,
the app service principal when deployed.
"""

import functools

from databricks_agentkit import workspace_client
from databricks_langchain import VectorSearchRetrieverTool

INDEX_NAME = "<catalog>.<schema>.<prefix>_chunks_index"
NUM_RESULTS = 8
# Metadata columns from module 02, mapped to the values the model may filter on. Use None for
# open-ended columns such as years, or for columns with more than 50 distinct values.
FILTER_VALUES: dict[str, list[str] | None] = {
    # "company": ["APPLE", "NETFLIX", ...],
    # "document_type": ["10K", "10Q", "8K", "EARNINGS"],
    # "fiscal_year": None,
}
CORPUS_DESCRIPTION = "<one sentence: what these documents are, e.g. 'SEC filings (10-K, 10-Q, 8-K, earnings releases) for 40 US public companies'>"


def _description() -> str:
    lines = [
        f"Search {CORPUS_DESCRIPTION}. Returns passages with file_name and page_numbers for citations.",
        "Call it once per distinct entity, period, or sub-question; issue several calls in parallel for comparisons.",
    ]
    if FILTER_VALUES:
        lines.append(
            "Use filters only when the question names a value exactly; otherwise search without filters. "
            "Filter on a list to match any of several values."
        )
        for column, values in FILTER_VALUES.items():
            allowed = ", ".join(repr(v) for v in values) if values else "any value of the column's type"
            lines.append(f"- {column}: {allowed}")
    return "\n".join(lines)


@functools.cache
def doc_search_tool() -> VectorSearchRetrieverTool:
    return VectorSearchRetrieverTool(
        index_name=INDEX_NAME,
        tool_name="search_documents",
        tool_description=_description(),
        num_results=NUM_RESULTS,
        query_type="HYBRID",  # keyword + semantic: exact names, tickers, and IDs still match
        columns=["chunk_id", "file_name", "page_numbers", *FILTER_VALUES],
        doc_uri="source_uri",
        primary_key="chunk_id",
        dynamic_filter=bool(FILTER_VALUES),
        workspace_client=workspace_client(),
    )
