"""Evaluate the doc-qa agent locally with MLflow GenAI judges (forge-doc-qa-agent module 03).

Copy to the Agent Bricks project root and run inside the project environment. $EXP is the
[tracing] experiment_name from agent.toml.

    uv run python evaluate.py --experiment "$EXP" --eval-file evals.jsonl

Eval file format is JSON Lines: {"question": str, "expected_response"?: str, "expected_facts"?: [str]}.
forge-unstructured-doc-extraction module 03 writes one (its file_name and pages keys are ignored here).
The script exits non-zero when a metric misses its threshold, so CI can gate on it.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import mlflow
from dotenv import load_dotenv

THRESHOLDS = {"retrieval_groundedness": 0.8, "relevance_to_query": 0.8, "correctness": 0.6}
CITATION_GUIDELINE = (
    "Every factual claim in the response is followed by a citation like [file_name, p. N], "
    "or the response clearly states that the documents do not contain the answer."
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--experiment", required=True, help="MLflow experiment path, e.g. the agent's tracing experiment")
    p.add_argument("--eval-file", required=True, type=Path)
    p.add_argument("--limit", type=int, default=0, help="Evaluate only the first N rows (0 = all)")
    return p.parse_args()


def load_eval_set(path: Path, limit: int) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    rows = rows[:limit] if limit else rows
    data = []
    for row in rows:
        record = {"inputs": {"messages": [{"role": "user", "content": row["question"]}]}}
        expectations = {k: row[k] for k in ("expected_response", "expected_facts") if row.get(k)}
        if expectations:
            record["expectations"] = expectations
        data.append(record)
    return data


def make_predict_fn():
    import agent.agent as agent_module

    # Each eval question is a single turn, so skip the bound Session Store: its client must not be
    # shared across the event loops that evaluate's worker threads create.
    agent_module.checkpointer = lambda: None

    async def answer(messages: list[dict]) -> str:
        graph = await agent_module.create_agent_graph("eval")
        result = await graph.ainvoke({"messages": messages})
        content = result["messages"][-1].content
        if isinstance(content, list):  # content blocks
            content = "".join(block.get("text", "") for block in content if isinstance(block, dict))
        return content

    def predict_fn(messages: list[dict]) -> str:
        return asyncio.run(answer(messages))

    return predict_fn


def main() -> None:
    args = parse_args()
    load_dotenv(Path(__file__).parent / ".env", override=False)

    from mlflow.genai.scorers import Correctness, Guidelines, RelevanceToQuery, RetrievalGroundedness, Safety

    mlflow.set_tracking_uri("databricks")
    mlflow.set_experiment(args.experiment)
    mlflow.langchain.autolog()  # LangGraph + retriever spans; RetrievalGroundedness needs the RETRIEVER span

    data = load_eval_set(args.eval_file, args.limit)
    scorers = [
        RelevanceToQuery(),
        RetrievalGroundedness(),
        Safety(),
        Guidelines(name="cites_sources", guidelines=CITATION_GUIDELINE),
    ]
    if data and all("expectations" in row for row in data):
        scorers.append(Correctness())
    else:
        print("some rows have no expected_response/expected_facts; skipping Correctness")

    results = mlflow.genai.evaluate(data=data, predict_fn=make_predict_fn(), scorers=scorers)
    print(json.dumps({"run_id": results.run_id, "metrics": results.metrics}, indent=2))

    failures = []
    for scorer in scorers:
        floor = THRESHOLDS.get(scorer.name)
        if floor is None:
            continue
        # Aggregates are logged as "<assessment name>/mean"; the assessment name may carry a prefix.
        key = f"{scorer.name}/mean"
        value = next((v for k, v in results.metrics.items() if k == key or k.endswith(f"/{key}")), None)
        if value is None:
            failures.append(f"{scorer.name}: no aggregate metric logged")
        elif value < floor:
            failures.append(f"{scorer.name}={value:.2f} < {floor}")
    if failures:
        print("FAILED thresholds: " + "; ".join(failures))
        sys.exit(1)
    print("all thresholds met")


if __name__ == "__main__":
    main()
