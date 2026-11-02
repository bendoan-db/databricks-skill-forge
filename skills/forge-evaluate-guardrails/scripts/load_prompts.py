# Databricks notebook source
# MAGIC %md
# MAGIC # Load the evaluation prompts
# MAGIC
# MAGIC The `load_prompts` task of the forge-evaluate-guardrails evaluation job: it loads the safety dataset, the default
# MAGIC Aegis 2.0 and ToxicChat test splits or the user's own, into the prompts table, one row per prompt labelled BLOCK or
# MAGIC ALLOW. The job passes its settings as the widgets at the top; to run it by hand, fill them in. To keep a change you
# MAGIC make here, export this notebook into the bundle's `src/` folder before the next deploy, which otherwise replaces
# MAGIC it (module 01).

# COMMAND ----------

"""Load the safety dataset into <prefix>_prompts (forge-evaluate-guardrails module 01).

First task of the evaluation job, before run_eval.py. It's a Databricks notebook: it runs as a serverless notebook
task whose parameters arrive as widgets, or locally through Databricks Connect, with the same settings as flags:

    uv run --with "databricks-connect>=17" python load_prompts.py --catalog main --schema guardrail_eval \
        --prefix guardrail_eval --config eval_config.json

The dataset comes from eval_config.json (render.py writes it from guardrail-eval.json):
- `default`: the test splits of nvidia/Aegis-AI-Content-Safety-Dataset-2.0 and lmsys/toxic-chat (toxicchat0124), each
  pinned to a commit and checked against its sha256. They're staged in the volume <prefix>_datasets and downloaded from
  Hugging Face only when they aren't there yet, so a workspace without egress can stage them with `databricks fs cp`.
- `table` or `file`: the user's own, with a prompt column, a label column whose block_values mean "should be blocked",
  and an optional category column.

Every dataset is reduced the same way: prompts stripped, empty ones dropped, repeats of a prompt merged (their
categories combined), and a prompt whose repeats disagree on the label dropped. Aegis prompts that read "REDACTED" are
dropped: the dataset doesn't distribute them. The table is replaced on every load, and a prompt keeps its prompt_id
(a hash of its dataset and text), so results from earlier runs still join.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DATASETS = {
    "aegis2": {
        "repo": "nvidia/Aegis-AI-Content-Safety-Dataset-2.0",
        "revision": "d86bb8bedff51d25ac834ab7838f1cc61acb7a2c",
        "file": "test.json",
        "sha256": "b0a6d602260524866053cb34105194f074f2c2906e3691b68d43b9e6e9318f35",
        "license": "CC-BY-4.0",
    },
    "toxicchat": {
        "repo": "lmsys/toxic-chat",
        "revision": "29df8e4dba60e1f4af4b4075c0705c5b313548a8",
        "file": "data/0124/toxic-chat_annotation_test.csv",
        "sha256": "3c2e49889626f7738dca0a29bface0ba0a0595b2ffdd17f0e02f19df7c3c4c9b",
        "license": "CC-BY-NC-4.0",
    },
}
DATASET_URL = "https://huggingface.co/datasets/{repo}/resolve/{revision}/{file}"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PREFIX_RE = re.compile(r"^[a-z0-9_]+$")
TABLE_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
SOURCES = ("default", "table", "file")
FORMATS = ("csv", "json", "jsonl", "parquet")
BLOCK, ALLOW = "BLOCK", "ALLOW"
REDACTED = "REDACTED"  # Aegis 2.0 prompts from the Suicide Detection dataset, which it doesn't distribute
PROMPTS_DDL = ("prompt_id STRING, dataset STRING, prompt STRING, expected STRING, categories ARRAY<STRING>, "
               "source_ref STRING, loaded_at TIMESTAMP")
STATS = ("rows_read", "kept", "block", "allow", "redacted", "empty", "duplicate", "conflicting")

# COMMAND ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])  # a notebook has no __doc__
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--prefix", required=True, help="Table prefix: writes <prefix>_prompts")
    p.add_argument("--config", default="", help="eval_config.json, which render.py writes (default: next to this file)")
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


def script_dir() -> Path:
    """This file's folder: next to the script, or a notebook's own workspace folder, its working directory."""
    return Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()


def load_config(path: Path) -> dict:
    """eval_config.json without render.py's checksum key."""
    if not path.is_file():
        raise SystemExit(f"{path} not found: render.py writes it into the bundle's src/ (module 01)")
    return {k: v for k, v in json.loads(path.read_text()).items() if k != "_rendered_by"}


def validate_dataset(spec) -> list[str]:
    """Every problem with the dataset spec in eval_config.json."""
    if not isinstance(spec, dict) or spec.get("source") not in SOURCES:
        return [f"dataset.source must be one of {list(SOURCES)}"]
    errors = []
    if spec["source"] == "default":
        unknown = sorted(set(spec.get("defaults") or DEFAULT_DATASETS) - set(DEFAULT_DATASETS))
        if unknown:
            errors.append(f"dataset.defaults: unknown {unknown}; use some of {sorted(DEFAULT_DATASETS)}")
        return errors
    if spec["source"] == "table" and not TABLE_RE.match(str(spec.get("table", ""))):
        errors.append(f"dataset.table={spec.get('table')!r}: use <catalog>.<schema>.<table>")
    if spec["source"] == "file":
        if not str(spec.get("path", "")).startswith("/Volumes/"):
            errors.append(f"dataset.path={spec.get('path')!r}: use a file in a volume, /Volumes/<catalog>/<schema>/<volume>/...")
        if spec.get("format") not in FORMATS:
            errors.append(f"dataset.format={spec.get('format')!r}: use one of {list(FORMATS)}")
    for key in ("prompt_column", "label_column"):
        if not str(spec.get(key) or "").strip():
            errors.append(f"dataset.{key} is missing")
    values = spec.get("block_values")
    if not (isinstance(values, list) and values and all(str(v).strip() for v in values)):
        errors.append("dataset.block_values must be a non-empty list: the label values that mean the prompt should be blocked")
    if spec.get("name") is not None and not PREFIX_RE.match(str(spec["name"])):
        errors.append(f"dataset.name={spec['name']!r}: lowercase letters, digits, and '_'")
    return errors

# COMMAND ----------

def prompt_id(dataset: str, prompt: str) -> str:
    """A prompt's stable id: the same dataset and text always get the same id, so runs join row for row."""
    return hashlib.sha256(f"{dataset}\n{prompt}".encode()).hexdigest()[:16]


def split_categories(value) -> list[str]:
    """Categories from a list, or from a comma-separated string; empty ones dropped."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [str(item).strip() for item in items if item is not None and str(item).strip()]


def block_label(value, block_values) -> bool:
    """True when a label value is one of the values that mean "should be blocked" (compared as lowercase text)."""
    return value is not None and str(value).strip().lower() in {str(v).strip().lower() for v in block_values}


def aegis_records(rows: list[dict]) -> tuple[list[tuple], int]:
    """(prompt, expected, categories, source_ref) for Aegis 2.0 rows, and how many were REDACTED (and so dropped)."""
    records, redacted = [], 0
    for row in rows:
        prompt = row.get("prompt") or ""
        if prompt.strip() == REDACTED:
            redacted += 1
            continue
        unsafe = str(row.get("prompt_label") or "").strip().lower() == "unsafe"
        records.append((prompt, BLOCK if unsafe else ALLOW,
                        split_categories(row.get("violated_categories")) if unsafe else [], str(row.get("id") or "")))
    return records, redacted


def toxicchat_records(rows: list[dict]) -> list[tuple]:
    """(prompt, expected, categories, source_ref) for ToxicChat rows: toxic ones are BLOCK, tagged jailbreaking too
    when they are."""
    records = []
    for row in rows:
        toxic = str(row.get("toxicity") or "").strip() == "1"
        jailbreak = str(row.get("jailbreaking") or "").strip() == "1"
        categories = (["toxicity"] if toxic else []) + (["jailbreaking"] if jailbreak else [])
        records.append((row.get("user_input") or "", BLOCK if toxic else ALLOW, categories, str(row.get("conv_id") or "")))
    return records


def custom_records(rows: list[dict], spec: dict) -> list[tuple]:
    """(prompt, expected, categories, source_ref) for the user's dataset, mapped by the spec's columns."""
    category = spec.get("category_column")
    records = []
    for number, row in enumerate(rows, 1):
        prompt = row.get(spec["prompt_column"])
        records.append(("" if prompt is None else str(prompt),
                        BLOCK if block_label(row.get(spec["label_column"]), spec["block_values"]) else ALLOW,
                        split_categories(row.get(category)) if category else [], str(number)))
    return records


def normalize(dataset: str, records: list[tuple], redacted: int = 0) -> tuple[list[dict], dict]:
    """The prompts table's rows for one dataset, and what happened to its rows. Prompts are stripped and empty ones
    dropped; repeats of a prompt are merged, their categories combined; a prompt whose repeats disagree is dropped."""
    stats = dict.fromkeys(STATS, 0)
    stats["rows_read"] = len(records) + redacted
    stats["redacted"] = redacted
    merged: dict[str, dict] = {}
    for prompt, expected, categories, ref in records:
        text = (prompt or "").strip()
        if not text:
            stats["empty"] += 1
            continue
        if text in merged:
            stats["duplicate"] += 1
            entry = merged[text]
            entry["expected"].add(expected)
            entry["categories"] += [c for c in categories if c not in entry["categories"]]
        else:
            merged[text] = {"expected": {expected}, "categories": list(dict.fromkeys(categories)), "source_ref": ref}
    rows = []
    for text, entry in merged.items():
        if len(entry["expected"]) > 1:
            stats["conflicting"] += 1
            continue
        expected = next(iter(entry["expected"]))
        stats["block" if expected == BLOCK else "allow"] += 1
        rows.append({"prompt_id": prompt_id(dataset, text), "dataset": dataset, "prompt": text, "expected": expected,
                     "categories": entry["categories"], "source_ref": entry["source_ref"]})
    stats["kept"] = len(rows)
    return rows, stats


def parse_default(key: str, data: bytes) -> list[dict]:
    """A default dataset's rows from its file: Aegis 2.0's test.json (a JSON array), ToxicChat's CSV."""
    text = data.decode("utf-8-sig")
    if key == "aegis2":
        return json.loads(text)
    csv.field_size_limit(10_000_000)
    return list(csv.DictReader(io.StringIO(text, newline="")))


def default_records(key: str, rows: list[dict]) -> tuple[list[tuple], int]:
    return aegis_records(rows) if key == "aegis2" else (toxicchat_records(rows), 0)

# COMMAND ----------

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checksum_status(data: bytes | None, expected: str) -> str:
    """'missing' when there's no file, 'ok' when its sha256 is the expected one, else 'mismatch'."""
    if data is None:
        return "missing"
    return "ok" if sha256(data) == expected else "mismatch"


def dataset_url(spec: dict) -> str:
    return DATASET_URL.format(**spec)


def staged_path(volume_dir: str, key: str) -> str:
    return f"{volume_dir.rstrip('/')}/{key}/{Path(DEFAULT_DATASETS[key]['file']).name}"


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "forge-evaluate-guardrails"})
    with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310 - a fixed https URL
        return response.read()


class VolumeFiles:
    """Reads and writes files in a volume: through /Volumes on Databricks compute, else through the Files API."""

    def __init__(self):
        self.fuse = "DATABRICKS_RUNTIME_VERSION" in os.environ
        self._w = None

    def _client(self):
        if self._w is None:
            from databricks.sdk import WorkspaceClient

            self._w = WorkspaceClient()
        return self._w

    def read(self, path: str) -> bytes | None:
        if self.fuse:
            return Path(path).read_bytes() if Path(path).is_file() else None
        try:
            return self._client().files.download(path).contents.read()
        except Exception as e:  # noqa: BLE001 - the SDK's NotFound, matched by name so the SDK stays optional
            if type(e).__name__ == "NotFound":
                return None
            raise

    def write(self, path: str, data: bytes) -> None:
        if self.fuse:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            partial = Path(f"{path}.part")
            partial.write_bytes(data)
            os.replace(partial, path)  # never leave a half-written file under the real name
        else:
            self._client().files.upload(path, io.BytesIO(data), overwrite=True)


def stage_default(files, volume_dir: str, key: str, fetch=download) -> bytes:
    """A default dataset's file, checked against its sha256: the copy staged in the volume, or a download from Hugging
    Face, written to the volume once it checks out."""
    spec = DEFAULT_DATASETS[key]
    path = staged_path(volume_dir, key)
    data = files.read(path)
    status = checksum_status(data, spec["sha256"])
    if status == "ok":
        return data
    if status == "mismatch":
        raise SystemExit(
            f"{path} has sha256 {sha256(data)}, not {spec['sha256']}: it isn't {spec['repo']} at {spec['revision']}. "
            f"Replace it with {dataset_url(spec)}, or remove it so this task downloads it."
        )
    url = dataset_url(spec)
    try:
        data = fetch(url)
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit(
            f"Can't download {url} ({e}). If this compute can't reach huggingface.co, stage the file yourself: "
            f"curl -L -o {Path(path).name} '{url}' && databricks fs cp {Path(path).name} dbfs:{path}"
        ) from e
    if checksum_status(data, spec["sha256"]) != "ok":
        raise SystemExit(f"{url} downloaded with sha256 {sha256(data)}, not {spec['sha256']}: "
                         "the file changed or the download was cut short. Nothing was written.")
    files.write(path, data)
    return data

# COMMAND ----------

def get_spark():
    if "DATABRICKS_RUNTIME_VERSION" in os.environ:
        from pyspark.sql import SparkSession

        return SparkSession.builder.getOrCreate()
    from databricks.connect import DatabricksSession

    return DatabricksSession.builder.serverless().getOrCreate()


def read_custom(spark, spec: dict) -> list[dict]:
    """The user's dataset's rows, with only the mapped columns."""
    from pyspark.sql import functions as F

    if spec["source"] == "table":
        df = spark.table(spec["table"])
    elif spec["format"] == "csv":
        df = spark.read.option("header", "true").option("multiLine", "true").option("escape", '"').csv(spec["path"])
    elif spec["format"] == "json":
        df = spark.read.option("multiLine", "true").json(spec["path"])
    elif spec["format"] == "jsonl":
        df = spark.read.json(spec["path"])
    else:
        df = spark.read.parquet(spec["path"])
    columns = [spec["prompt_column"], spec["label_column"]] + ([spec["category_column"]] if spec.get("category_column") else [])
    if missing := [c for c in columns if c not in df.columns]:
        raise SystemExit(f"{spec.get('table') or spec.get('path')} has no column {missing}; its columns are {df.columns}")
    return [row.asDict(recursive=True) for row in df.select(*[F.col(f"`{c}`") for c in columns]).collect()]


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    for name in (args.catalog, args.schema):
        if not NAME_RE.match(name):
            raise SystemExit(f"Invalid name {name!r}: use letters, digits, '_' or '-'")
    if not PREFIX_RE.match(args.prefix):
        raise SystemExit(f"Invalid prefix {args.prefix!r}: use lowercase letters, digits, and '_'")
    spec = load_config(Path(args.config) if args.config else script_dir() / "eval_config.json").get("dataset")
    if errors := validate_dataset(spec):
        raise SystemExit("eval_config.json: " + "; ".join(errors))

    spark = get_spark()
    table = f"`{args.catalog}`.`{args.schema}`.`{args.prefix}_prompts`"
    rows, summary = [], {}
    if spec["source"] == "default":
        spark.sql(f"CREATE VOLUME IF NOT EXISTS `{args.catalog}`.`{args.schema}`.`{args.prefix}_datasets`")
        volume_dir = f"/Volumes/{args.catalog}/{args.schema}/{args.prefix}_datasets"
        files = VolumeFiles()
        for key in spec.get("defaults") or list(DEFAULT_DATASETS):
            records, redacted = default_records(key, parse_default(key, stage_default(files, volume_dir, key)))
            kept, summary[key] = normalize(key, records, redacted)
            rows += kept
    else:
        name = spec.get("name") or "custom"
        kept, summary[name] = normalize(name, custom_records(read_custom(spark, spec), spec))
        rows += kept
    if not rows:
        raise SystemExit(f"No prompts to evaluate: {json.dumps(summary)}")

    loaded_at = datetime.now(timezone.utc).replace(tzinfo=None)
    data = [(r["prompt_id"], r["dataset"], r["prompt"], r["expected"], r["categories"], r["source_ref"], loaded_at)
            for r in rows]
    spark.createDataFrame(data, PROMPTS_DDL).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(table)
    print(json.dumps({
        "table": table.replace("`", ""),
        "prompts": len(rows),
        "block": sum(r["expected"] == BLOCK for r in rows),
        "allow": sum(r["expected"] == ALLOW for r in rows),
        "datasets": summary,
    }))

# COMMAND ----------

# As a job's notebook task, the settings arrive as widgets; run as a script, as command-line flags.
if __name__ == "__main__":
    main(notebook_argv(build_parser()) if "dbutils" in globals() else None)
