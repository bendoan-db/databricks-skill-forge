"""The two LLM steps, through ai_query on the job's serverless Spark: assess (does an upstream change matter to a
Forge skill?) and propose (edit the Forge skill). Prompts live in prompts/*.md; PROMPT_VERSION is recorded with
every result, so a changed prompt shows up in the tables.
"""

import json
import re
from pathlib import Path

from .config import MODEL_RE

PROMPT_VERSION = "2026-10-26.1"
PROMPTS = Path(__file__).resolve().parent / "prompts"
MATERIALITY = ("none", "compatible", "material", "breaking")
BUMPS = ("patch", "minor", "major")
MAX_PROMPT_CHARS = 350_000  # well inside the models' context, with room for the answer


def template(name: str, **values: str) -> str:
    """Fill a prompt's {{key}} slots. Plain replace, so code and JSON in the values need no escaping."""
    text = (PROMPTS / f"{name}.md").read_text()
    # Checked on the template itself: filled values (diffs, skill files) can contain {{...}} of their own.
    if missing := sorted(set(re.findall(r"\{\{(\w+)\}\}", text)) - set(values)):
        raise KeyError(f"prompts/{name}.md needs values for {missing}")
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), text)


def fit(sections: list[tuple[str, str]], budget: int = MAX_PROMPT_CHARS) -> str:
    """Join (title, text) sections, cutting the last ones that don't fit the budget."""
    out, used = [], 0
    for title, text in sections:
        block = f"### {title}\n\n{text.rstrip()}\n"
        if used + len(block) > budget:
            out.append(f"### {title}\n\n(cut: the prompt reached its size budget)\n")
            continue
        out.append(block)
        used += len(block)
    return "\n".join(out)


def parse_json(text: str | None) -> dict:
    """The JSON object in a model response, with or without a code fence."""
    if not text:
        raise ValueError("empty response")
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("the response isn't a JSON object")
    return value


def check_assessment(value: dict) -> dict:
    if value.get("materiality") not in MATERIALITY:
        raise ValueError(f"materiality must be one of {MATERIALITY}, got {value.get('materiality')!r}")
    value.setdefault("summary", "")
    value["affected_modules"] = [str(m) for m in value.get("affected_modules") or []]
    value["resolved_gaps"] = [str(g) for g in value.get("resolved_gaps") or []]
    return value


def check_proposal(value: dict) -> dict:
    if value.get("bump") not in BUMPS:
        raise ValueError(f"bump must be one of {BUMPS}, got {value.get('bump')!r}")
    if not isinstance(value.get("edits"), list):
        raise ValueError("edits must be a list (empty when no edit is needed)")
    value.setdefault("summary", "")
    value.setdefault("rationale", [])
    return value


def ai_query(spark, model: str, prompts: list[str], max_tokens: int) -> list[tuple[str | None, str | None]]:
    """[(response, error)] for each prompt, in one batched query. failOnError => false keeps one bad row from
    failing the batch."""
    if not MODEL_RE.match(model):
        raise ValueError(f"model {model!r} isn't a system.ai model name")
    if not prompts:
        return []
    spark.createDataFrame(list(enumerate(prompts)), "id INT, prompt STRING").createOrReplaceTempView("forge_drift_prompts")
    rows = spark.sql(
        f"""SELECT id, ai_query('{model}', prompt,
                failOnError => false,
                responseFormat => '{{"type":"json_object"}}',
                modelParameters => named_struct('max_tokens', {int(max_tokens)})) AS out
            FROM forge_drift_prompts"""
    ).collect()
    by_id = {r["id"]: (r["out"]["response"], r["out"]["errorMessage"]) for r in rows}
    return [by_id.get(i, (None, "no row returned")) for i in range(len(prompts))]
