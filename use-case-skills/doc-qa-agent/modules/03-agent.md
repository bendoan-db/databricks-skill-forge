# 03 · Agent

Scaffold a LangGraph agent with the Agent Bricks CLI, give it the document search tool and a grounding prompt, and test it locally.

**Docs:** no base skill covers the Agent Bricks CLI yet. Use these pages, and the files that `agentbricks init` generates:
- [Agent Bricks CLI](https://docs.databricks.com/aws/en/agents/custom-agents/agent-bricks-cli)
- [Agent Server](https://docs.databricks.com/aws/en/agents/deploy/agent-server)
- [Agent memory and sessions](https://docs.databricks.com/aws/en/agents/custom-agents/stateful-agents)

The generated `AGENTKIT_CONTRACT.md` and `AGENTS.md` are versioned with the CLI. When they disagree with this module, they win. Note the disagreement in your final report so the skill owner can update this module.

## Inputs → outputs

- **Inputs:** `name`, `chat_model`, `filter_values`, and the index from `doc-qa.json`.
- **Outputs:** the `<name>/` project, which answers with citations under `agentbricks dev` and passes `agentbricks doctor .`.

## 1. Scaffold

```bash
cd <workdir>
agentbricks init --framework langgraph --session-store <name>-sessions <name>
cd <name>
agentbricks memory unbind     # init declares a memory store by default; doc Q&A keeps conversation state only
```

`init` writes these files:
- `agent.toml`: the declarative resources, including the session store and tracing.
- `agent/`: your code.
- `runtime/`: the DurableAgentServer adapter. Leave it alone.
- `app.yaml` and a chat UI.

Tracing binds to `/Shared/agentbricks_traces/<name>-<token>`. Read the exact path from `[tracing] experiment_name` in `agent.toml`, and record it as `experiment` in `doc-qa.json`; module 05 uses it.

The session store gives durable multi-turn state (managed, Lakebase-backed). Keep the memory binding only if the user asked for cross-conversation memory (see the decision table).

## 2. Add retrieval

1. Write a one-sentence `corpus_description` to `doc-qa.json` that says what the documents are, for example `"SEC filings (10-K, 10-Q, 8-K, earnings releases) for 40 US public companies"`. Then render the tool:

   ```bash
   uv run <skill>/scripts/render.py agent --state <workdir>/doc-qa.json
   ```

   This writes `agent/retrieval.py` with the index, `filter_values`, `corpus_description`, and `num_results` (default 8) filled in. Without `filter_values`, filtering is off. To change the tool beyond those settings, **load** `databricks-vector-search` (its `VectorSearchRetrieverTool` note).
2. Delete the template's sample tools and the two tests that assert them:
   - `agent/tools/sample_tool.py`
   - `agent/tools/send_message.py`
   - In `tests/test_agent.py`: `test_tools_autoregister` and `test_gated_tool_is_in_require_approval`. Keep the other tests. The interrupt-serialization test uses a literal payload and doesn't need the tool.

   Keep `agent/tools/__init__.py`. It's how you add local tools later.

## 3. Edit `agent/agent.py`

Make exactly these changes and leave the rest of the template intact:

```python
from datetime import date

from agent.retrieval import doc_search_tool

MODEL = "<chat_model>"                   # from doc-qa.json; default system.ai.claude-sonnet-5-5
REQUIRE_APPROVAL = {}                    # read-only agent: no tool needs human approval

SYSTEM_PROMPT = """You answer questions using only the documents returned by the search_documents tool.

- Search before answering. Split multi-part or comparative questions into separate searches and run them in parallel.
- Use filters when the question names an entity, type, or period; if a filtered search returns nothing, retry without filters.
- Cite every factual claim as [file_name, p. N] using the file_name and page_numbers of the passage it came from.
- If the documents don't contain the answer, say so and summarize what you did find. Never fill gaps from memory.
- Show the arithmetic for any number you calculate, and give units and periods exactly as the source states them.
- Today's date is {today}. Resolve relative dates ("last year", "latest") against it and the document dates.
"""
```

In `create_agent_graph`, add the tool and pass the prompt:

```python
    tools = [
        doc_search_tool(),
        *all_tools(),
        *memory_tools(actor),
        *genie_tools(workspace_client_for=workspace_client_for),
        *mcp,
    ]
    ...
    return create_agent(
        model=...,                                   # unchanged
        tools=tools,
        system_prompt=SYSTEM_PROMPT.format(today=date.today().isoformat()),
        middleware=middleware,
        checkpointer=checkpointer(),
    )
```

The graph is rebuilt for every turn, so the date stays current. `doc_search_tool()` is cached, so the index is described once per process.

Keep a single tool-calling agent: current models split questions and call the tool in parallel on their own. Add a planner node only if module 05 shows multi-hop failures that prompt changes don't fix. In that case, add a node before the agent that writes 2–5 explicit sub-questions into the messages.

## 4. Run and test locally

```bash
agentbricks doctor .            # static check: agent.toml, DurableAgentServer entrypoint, framework adapter
uv run pytest -q                # template tests, minus the deleted sample-tool tests
agentbricks dev                 # http://localhost:8000 (chat UI + /api/invocations), your CLI identity
```

```bash
agentbricks endpoint invoke --url http://localhost:8000 --path /api/invocations \
  --json "{\"id\":\"$(uuidgen)\",\"session_id\":\"$(uuidgen)\",\"input\":{\"messages\":[{\"role\":\"user\",\"content\":\"<a question the corpus answers>\"}]}}"
```

Ask three kinds of questions:
1. One the corpus answers directly.
2. One that needs two searches, such as a comparison across entities or years.
3. One the corpus can't answer.

## Verify

- Question 1 cites at least one `[file_name, p. N]` that really holds the answer. Open the file to check.
- The trace for question 2 shows two or more `search_documents` calls, with filters when metadata exists. Tracing goes to the bound experiment; `agentbricks dev` traces locally.
- Question 3 says the answer isn't in the documents.
- A follow-up in the same `session_id` ("and the year before?") resolves against the previous turn.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `PERMISSION_DENIED` on `system.ai.*` | Caller needs `EXECUTE` on the model service; **load** `databricks-unity-gateway` `references/permissions.md` |
| 403 "no longer available" on a `databricks-*` name | Legacy endpoint retired; use the `system.ai.*` name |
| Agent answers without searching | Tool missing from `tools`, or prompt edit lost; check the trace for a `search_documents` span |
| `Invalid filter` / zero hits with filters | Value casing differs from the data; regenerate `filter_values` from the chunks table and render again |
| Retrieval returns table summaries / "Related questions" text | Expected: the tool returns `chunk_to_embed`; see module 05 for tuning |
