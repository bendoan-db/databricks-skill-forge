# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Inventory a codebase for forge-smith (module 01): its files, bundles and jobs, the Databricks features it uses
(mapped to base skills), and every setting it takes as a parameter or hard-codes. It reads files only: it never
runs, imports, or installs the code it scans.

    uv run <smith>/scripts/scan.py <codebase> --out <workdir>/smith-scan.json [--skills-dir <dir>]

--skills-dir (default: the folder holding forge-smith) adds the installed skills: each base skill's version,
description, and reference files with their first heading, so module 02 can choose Load lines without reading
every skill, and each Forge use-case skill's catalog entry, so module 02 can check for overlap. Locations are
`path:line`; in a .ipynb, lines count through the code cells joined in order. Hard-coded tokens are reported by
location only, never by value.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

SMITH_DIR = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".databricks", "dist", "build", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", ".ipynb_checkpoints", ".idea", ".vscode"}
MAX_BYTES = 1_000_000
MAX_FILES = 5_000
MAX_WHERE = 5  # locations kept per finding
MAX_DATA_FILES = 50
DATA_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".doc", ".docx", ".ppt", ".pptx",
                 ".xls", ".xlsx", ".csv", ".tsv", ".parquet", ".avro", ".orc", ".zip", ".gz", ".tar", ".whl", ".jar",
                 ".dbc", ".pkl", ".npy", ".h5"}  # never read as text; listed as data_files
NOTEBOOK_HEADER = re.compile(r"^(#|--|//) Databricks notebook source")
REQUIREMENTS = re.compile(r"^(requirements.*\.txt|pyproject\.toml|setup\.py|setup\.cfg|environment\.ya?ml|Pipfile)$")
CODE_KINDS = {"notebook", "python", "sql", "bundle", "config", "app", "dashboard", "frontend", "shell", "requirements"}
PACKAGES = {"databricks-sdk", "databricks-connect", "databricks-vectorsearch", "databricks-agents", "databricks-cli",
            "databricks-langchain", "databricks-sql-connector", "databricks-openai", "databricks-ai-bridge",
            "databricks-feature-engineering", "databricks-mcp", "databricks-agentbricks", "databricks-ai-search",
            "databricks-dlt", "databricks-labs"}

# (base skill, what it covers, pattern). A hint for module 02, not a verdict: it reads the evidence.
SIGNALS = [
    ("databricks-ai-functions", "AI Functions",
     r"\bai_(parse_document|extract|classify|query|prep_search|summarize|gen|forecast|mask|translate|analyze_sentiment|"
     r"similarity|fix_grammar)\s*\("),
    ("databricks-vector-search", "AI Search (Vector Search)",
     r"VectorSearchClient|databricks[-.]vectorsearch|create_delta_sync_index|similarity_search|vector[-_]search[-_]endpoint"),
    ("databricks-mlflow-evaluation", "MLflow GenAI evaluation",
     r"mlflow\.genai\.evaluate|mlflow\.evaluate\(|@scorer\b|mlflow\.genai\.scorers|RetrievalGroundedness"),
    ("databricks-pipelines", "Lakeflow Declarative Pipelines",
     r"(?m)^\s*import dlt\b|from pyspark import pipelines|@(dlt|dp)\.(table|view|materialized_view)|"
     r"(?i:CREATE\s+OR\s+REFRESH\s+(STREAMING\s+TABLE|MATERIALIZED\s+VIEW))"),
    ("databricks-spark-structured-streaming", "Structured Streaming",
     r"\.readStream\b|\.writeStream\b|trigger\(\s*(availableNow|processingTime)"),
    ("databricks-jobs", "Lakeflow Jobs",
     r"spark_python_task|notebook_task|python_wheel_task|sql_task|run_job_task|file_arrival|quartz_cron_expression|"
     r"jobs\.(create|submit|run_now)\("),
    ("databricks-dabs", "Declarative Automation Bundles", r"(?m)^bundle:\s*$|\$\{var\.[\w-]+\}"),
    ("databricks-apps-python", "a Python Databricks App",
     r"(?m)^\s*import (streamlit|gradio|dash)\b|^\s*from (fastapi|flask) import"),
    ("databricks-apps", "an AppKit Databricks App", r"@databricks/appkit"),
    ("databricks-model-serving", "Model Serving",
     r"serving_endpoints\.|ServingEndpoint|mlflow\.deployments|get_deploy_client|/serving-endpoints/"),
    ("databricks-ml-training", "ML training, or a custom agent",
     r"mlflow\.(sklearn|xgboost|lightgbm|pytorch|pyfunc)\.|(?m:^\s*import (xgboost|lightgbm|optuna)\b)|"
     r"FeatureEngineeringClient|FeatureLookup|ResponsesAgent|ChatAgent|langgraph"),
    ("databricks-metric-views", "Metric views", r"(?i:METRIC\s+VIEW)|metric_view"),
    ("databricks-aibi-dashboards", "AI/BI dashboards", r"lvdash|lakeview"),
    ("databricks-genie-agents", "Genie", r"genie_space|GenieSpace|/genie/spaces|genie\.(start_conversation|create_message)"),
    ("databricks-lakebase", "Lakebase", r"psycopg|lakebase|database_instance|synced_database_table"),
    ("databricks-unity-gateway", "Unity AI Gateway models", r"system\.ai\.[a-z0-9-]+|ai[-_]gateway|unity[-_]gateway"),
    ("databricks-unity-catalog", "Unity Catalog objects and grants",
     r"(?i:\bGRANT\s+\w+.*\bON\b|\bREVOKE\b|CREATE\s+(VOLUME|SCHEMA|CATALOG))|/Volumes/"),
    ("databricks-dbsql", "Databricks SQL", r"warehouse_id|statement_execution|(?i:CREATE\s+MATERIALIZED\s+VIEW)|read_files\("),
    ("databricks-synthetic-data-gen", "synthetic data", r"(?m)^\s*(from|import) (faker|dbldatagen)\b"),
    ("databricks-zerobus-ingest", "Zerobus Ingest", r"zerobus"),
    ("databricks-iceberg", "Iceberg", r"(?i:\biceberg\b)|universalFormat"),
    ("databricks-lakeflow-connect", "Lakeflow Connect", r"ingestion_definition|gateway_definition"),
    ("databricks-python-sdk", "the Databricks SDK", r"from databricks\.sdk\b|WorkspaceClient\("),
    ("databricks-serverless", "serverless compute", r"environment_version|(?m:^\s*environments:\s*$)"),
    ("databricks-serverless-migration", "classic compute (check what needs it)",
     r"node_type_id|new_cluster|num_workers|init_scripts|spark_jar_task|\.rdd\b|sparkContext"),
    ("databricks-agent-bricks", "Agent Bricks", r"agent_bricks|agentbricks|supervisor_agent"),
]
DEPRECATED = [  # Forge never builds or recommends these; module 02 replaces them
    ("Agent Bricks Knowledge Assistant", r"(?i:knowledge[_ -]?assistant)"),
    ("Agent Bricks Information Extraction", r"(?i:information[_ -]?extraction)"),
]
SETTINGS = [  # (kind, pattern); group "name" and optional "default"
    ("widget", r"dbutils\.widgets\.(?:text|dropdown|combobox|multiselect)\(\s*(?:name\s*=\s*)?['\"](?P<name>[^'\"]+)['\"]"
               r"(?:\s*,\s*(?:defaultValue\s*=\s*)?['\"](?P<default>[^'\"]*)['\"])?"),
    ("argument", r"add_argument\(\s*['\"](?P<name>--?[\w-]+)['\"](?:[^)\n]*?default\s*=\s*(?P<default>[^,)\n]+))?"),
    ("environment", r"os\.(?:environ\.get|getenv)\(\s*['\"](?P<name>\w+)['\"](?:\s*,\s*(?P<default>[^)\n]+))?\)"),
    ("environment", r"os\.environ\[\s*['\"](?P<name>\w+)['\"]\s*\]"),
    ("spark_conf", r"spark\.conf\.get\(\s*['\"](?P<name>[\w.]+)['\"](?:\s*,\s*(?P<default>[^)\n]+))?\)"),
    ("template", r"\{\{\s*(?P<name>[A-Za-z_]\w*)\s*\}\}"),
]
SECRET = re.compile(r"dbutils\.secrets\.get\(\s*(?:scope\s*=\s*)?['\"](?P<scope>[^'\"]+)['\"]\s*,\s*"
                    r"(?:key\s*=\s*)?['\"](?P<key>[^'\"]+)['\"]|\{\{secrets/(?P<scope2>[^/}]+)/(?P<key2>[^}]+)\}\}")
RESOURCES = [  # (kind, pattern); group "value"
    ("volume_path", r"(?P<value>/Volumes/[\w.-]+/[\w.-]+/[\w.-]+(?:/[^\s'\"`),;]*)?)"),
    ("dbfs_path", r"(?P<value>dbfs:/[^\s'\"`),;]+)"),
    ("table", r"(?i:\b(?:FROM|JOIN|INTO|TABLE|UPDATE|VIEW|MERGE[ \t]+INTO)[ \t]+)"
              r"`?(?P<value>[A-Za-z_]\w*`?\.`?[A-Za-z_]\w*`?\.`?[A-Za-z_]\w*)(?![\w`]*\()"),  # not a method call
    ("table", r"(?:\.table|saveAsTable|toTable|forName|read_table)\(\s*f?['\"](?P<value>[\w{}]+\.[\w{}]+\.[\w{}]+)['\"]"),
    ("model_or_endpoint", r"['\"`](?P<value>(?:databricks-[a-z0-9][a-z0-9-]*[a-z0-9]|system\.ai\.[a-z0-9-]+))['\"`]"),
    ("warehouse_id", r"warehouse_id['\"]?\s*[:=]\s*['\"]?(?P<value>[0-9a-f]{16})\b"),
    ("cluster", r"node_type_id['\"]?\s*[:=]\s*['\"]?(?P<value>[\w.-]+)"),
    ("schedule", r"quartz_cron_expression['\"]?\s*[:=]\s*['\"]?(?P<value>[^'\"\n]+)"),
    ("workspace_host", r"(?P<value>https://[\w.-]+\.(?:cloud\.databricks\.com|azuredatabricks\.net|gcp\.databricks\.com))"),
]
PY_IMPORT = re.compile(r"\s*from\s+[\w.]+\s+import\b")
TOKEN = re.compile(r"\bdapi[0-9a-f]{32}\b|\bdose[0-9a-f]{32}\b|\bghp_[A-Za-z0-9]{36}\b")


def kind_of(path: Path, text: str) -> str:
    name, suffix = path.name, path.suffix.lower()
    if suffix == ".ipynb" or (suffix in (".py", ".sql", ".scala", ".r") and NOTEBOOK_HEADER.match(text)):
        return "notebook"
    if REQUIREMENTS.match(name):
        return "requirements"
    if name in ("app.yaml", "app.yml"):
        return "app"
    if name.endswith(".lvdash.json"):
        return "dashboard"
    if suffix in (".yml", ".yaml"):
        return "bundle" if re.search(r"(?m)^(bundle|resources):\s*$", text) else "config"
    return {".py": "python", ".sql": "sql", ".json": "config", ".toml": "config", ".cfg": "config", ".ini": "config",
            ".jsonl": "data", ".ndjson": "data", ".md": "docs", ".rst": "docs", ".txt": "docs", ".sh": "shell", ".ts": "frontend", ".tsx": "frontend",
            ".js": "frontend", ".jsx": "frontend", ".scala": "scala", ".r": "r"}.get(suffix, "other")


def notebook_source(raw: str) -> str:
    """A .ipynb's code cells, joined in order."""
    try:
        cells = json.loads(raw).get("cells", [])
    except (json.JSONDecodeError, AttributeError):
        return raw
    return "\n".join("".join(c.get("source", [])) for c in cells if c.get("cell_type") == "code")


def read(path: Path) -> str | None:
    """The file's text, or None when it's binary or too large to scan."""
    data = path.read_bytes()
    if len(data) > MAX_BYTES or b"\0" in data[:8192]:
        return None
    text = data.decode("utf-8", errors="replace")
    return notebook_source(text) if path.suffix.lower() == ".ipynb" else text


def walk(root: Path) -> tuple[list[Path], dict[str, int]]:
    """Every regular file, without descending into vendored, cache, or VCS folders; symlinks aren't followed."""
    files, skipped = [], {"vendored_or_cache_dirs": 0}
    for folder, dirs, names in os.walk(root):
        skipped["vendored_or_cache_dirs"] += sum(1 for d in dirs if d in SKIP_DIRS)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        files += [Path(folder) / n for n in sorted(names) if not (Path(folder) / n).is_symlink()]
    if len(files) > MAX_FILES:
        skipped["over_file_limit"] = len(files) - MAX_FILES
        files = files[:MAX_FILES]
    return files, skipped


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


class Findings:
    """Findings keyed by identity, each with a count and its first few locations."""

    def __init__(self):
        self.items: dict[tuple, dict] = {}

    def add(self, identity: tuple, where: str, **fields) -> None:
        item = self.items.setdefault(identity, {**fields, "count": 0, "where": []})
        item["count"] += 1
        if len(item["where"]) < MAX_WHERE:
            item["where"].append(where)
        for name, value in fields.items():  # keep the first non-empty value seen
            if item.get(name) in (None, "") and value not in (None, ""):
                item[name] = value

    def list(self) -> list[dict]:
        return list(self.items.values())


def clean_default(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().rstrip(",").strip()
    return value.strip("'\"") if len(value) < 200 else value[:200]


def scan_text(rel: str, kind: str, text: str, signals: Findings, settings: Findings, resources: Findings,
              secrets: Findings, deprecated: Findings, tokens: list[str]) -> None:
    for skill, covers, pattern in SIGNALS:
        for m in re.finditer(pattern, text):
            signals.add((skill,), f"{rel}:{line_of(text, m.start())}", base_skill=skill, covers=covers)
    for product, pattern in DEPRECATED:
        for m in re.finditer(pattern, text):
            deprecated.add((product,), f"{rel}:{line_of(text, m.start())}", product=product)
    for setting_kind, pattern in SETTINGS:
        if setting_kind == "template" and kind not in ("sql", "notebook", "bundle", "config"):
            continue
        for m in re.finditer(pattern, text):
            default = clean_default(m.groupdict().get("default"))
            settings.add((setting_kind, m["name"]), f"{rel}:{line_of(text, m.start())}", kind=setting_kind,
                         name=m["name"], default=default)
    for m in SECRET.finditer(text):
        scope, key = m["scope"] or m["scope2"], m["key"] or m["key2"]
        secrets.add((scope, key), f"{rel}:{line_of(text, m.start())}", scope=scope, key=key)
    for resource_kind, pattern in RESOURCES:
        if resource_kind == "model_or_endpoint" and kind == "requirements":
            continue
        for m in re.finditer(pattern, text):
            value = m["value"].replace("`", "").rstrip(".")
            if resource_kind == "model_or_endpoint" and value in PACKAGES:
                continue
            if resource_kind == "table" and PY_IMPORT.match(text, text.rfind("\n", 0, m.start()) + 1):
                continue  # `from pyspark.sql.functions import ...` isn't a table
            resources.add((resource_kind, value), f"{rel}:{line_of(text, m.start())}", kind=resource_kind, value=value)
    tokens.extend(f"{rel}:{line_of(text, m.start())}" for m in TOKEN.finditer(text))


def bundle_resources(rel: str, text: str) -> dict | None:
    """Jobs (with task order), pipelines, apps, and variables in a bundle file; ${...} stays as text."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return {"file": rel, "error": f"not valid YAML: {str(e).splitlines()[0]}"}
    if not isinstance(doc, dict):
        return None
    out = {"file": rel}
    if isinstance(doc.get("bundle"), dict):
        out["bundle"] = doc["bundle"].get("name")
    if isinstance(doc.get("variables"), dict):
        out["variables"] = {name: {"default": (spec or {}).get("default") if isinstance(spec, dict) else spec,
                                   "description": (spec or {}).get("description") if isinstance(spec, dict) else None}
                            for name, spec in doc["variables"].items()}
    if isinstance(doc.get("targets"), dict):
        out["targets"] = sorted(doc["targets"])
    res = doc.get("resources") if isinstance(doc.get("resources"), dict) else {}
    jobs = {}
    for name, job in (res.get("jobs") or {}).items():
        tasks = []
        for task in (job or {}).get("tasks") or []:
            source = next((task[t].get(k) for t in task if isinstance(task.get(t), dict)
                           for k in ("notebook_path", "python_file", "path", "file", "main_class_name")
                           if task[t].get(k)), None) if isinstance(task, dict) else None
            tasks.append({"task_key": task.get("task_key"), "type": next((k for k in task if k.endswith("_task")), None),
                          "source": source, "depends_on": [d.get("task_key") for d in task.get("depends_on") or []]})
        jobs[name] = {"tasks": order(tasks), "parameters": [p.get("name") for p in (job or {}).get("parameters") or []],
                      "trigger": sorted(k for k in ("schedule", "trigger", "continuous") if (job or {}).get(k))}
    if jobs:
        out["jobs"] = jobs
    for key in ("pipelines", "apps", "model_serving_endpoints", "dashboards", "volumes", "schemas", "experiments"):
        if res.get(key):
            out[key] = sorted(res[key])
    return out if len(out) > 1 else None


def order(tasks: list[dict]) -> list[dict]:
    """Tasks in dependency order (a task after everything it depends on); a cycle keeps the file order."""
    done, ordered, pending = set(), [], list(tasks)
    while pending:
        ready = [t for t in pending if set(t["depends_on"]) <= done]
        if not ready:
            return ordered + pending
        for task in ready:
            ordered.append(task)
            done.add(task["task_key"])
            pending.remove(task)
    return ordered


def frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    try:
        return (yaml.safe_load(match.group(1)) or {}) if match else {}
    except yaml.YAMLError:
        return {}


def installed_skills(skills_dir: Path) -> tuple[dict, dict]:
    """(base skills, Forge use cases) installed in skills_dir."""
    base, forge = {}, {}
    if not skills_dir.is_dir():
        return base, forge
    for folder in sorted(p for p in skills_dir.iterdir() if (p / "SKILL.md").is_file()):
        meta = frontmatter((folder / "SKILL.md").read_text(errors="replace"))
        if folder.name.startswith("databricks-"):
            refs = {}
            for ref in sorted((folder / "references").glob("*.md")) if (folder / "references").is_dir() else []:
                heading = next((line.lstrip("# ").strip() for line in ref.read_text(errors="replace").splitlines()
                                if line.startswith("#")), "")
                refs[f"references/{ref.name}"] = heading
            base[folder.name] = {"version": str((meta.get("metadata") or {}).get("version") or "unversioned"),
                                 "description": str(meta.get("description") or ""), "references": refs}
        elif (folder / "ucs.yaml").is_file():
            ucs = yaml.safe_load((folder / "ucs.yaml").read_text()) or {}
            forge[folder.name] = {"version": str(ucs.get("version")), **(ucs.get("catalog") or {})}
    return base, forge


def git_source(root: Path) -> dict:
    """The repo's origin URL and HEAD commit, read from .git without running git."""
    git = root / ".git"
    if not git.is_dir():
        return {}
    out = {}
    config = (git / "config").read_text(errors="replace") if (git / "config").is_file() else ""
    if m := re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', config):
        out["repo"] = m.group(1)
    head = (git / "HEAD").read_text().strip() if (git / "HEAD").is_file() else ""
    if head.startswith("ref: "):
        ref = head[5:]
        if (git / ref).is_file():
            out["commit"] = (git / ref).read_text().strip()
        elif (git / "packed-refs").is_file():
            out["commit"] = next((line.split()[0] for line in (git / "packed-refs").read_text().splitlines()
                                  if line.endswith(" " + ref)), None)
    elif head:
        out["commit"] = head
    return out


def scan(root: Path, skills_dir: Path | None) -> dict:
    files, skipped = walk(root)
    signals, settings, resources, secrets, deprecated = Findings(), Findings(), Findings(), Findings(), Findings()
    inventory, bundles, docs, tokens, data_files, binary = [], [], [], [], [], 0
    for path in files:
        rel = path.relative_to(root).as_posix()
        if path.suffix.lower() in DATA_SUFFIXES:  # sample data and archives: what a certification scenario can use
            if len(data_files) < MAX_DATA_FILES:
                data_files.append({"path": rel, "bytes": path.stat().st_size})
            binary += 1
            continue
        text = read(path)
        if text is None:
            binary += 1
            continue
        kind = kind_of(path, text)
        inventory.append({"path": rel, "kind": kind, "lines": text.count("\n") + 1})
        if kind == "docs" and path.suffix.lower() == ".md":
            docs.append({"path": rel, "headings": [line.strip() for line in text.splitlines() if line.startswith("#")][:20]})
        if kind in CODE_KINDS:
            scan_text(rel, kind, text, signals, settings, resources, secrets, deprecated, tokens)
        if kind == "bundle" and (found := bundle_resources(rel, text)):
            bundles.append(found)
    skipped["binary_or_over_1mb"] = binary
    base, forge = installed_skills(skills_dir) if skills_dir else ({}, {})
    found_signals = sorted(signals.list(), key=lambda s: -s["count"])
    for s in found_signals:
        s["installed"] = s["base_skill"] in base if base else None
    numbered = [f["path"] for f in inventory if re.match(r"^\d+[_\-. ]", Path(f["path"]).name)]
    return {
        "root": str(root), "source": git_source(root),
        "files": inventory, "skipped": skipped, "data_files": data_files, "docs": docs, "numbered_files": numbered,
        "bundles": bundles,
        "signals": found_signals, "deprecated": deprecated.list(),
        "settings": settings.list(), "secrets": secrets.list(), "resources": resources.list(),
        "hard_coded_tokens": tokens, "base_skills": base, "forge_use_cases": forge,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("codebase", type=Path, help="Folder to scan (a clone, an extracted archive, or exported notebooks)")
    p.add_argument("--out", type=Path, required=True, help="Where to write the JSON inventory")
    p.add_argument("--skills-dir", type=Path, default=SMITH_DIR.parent, help="Folder with the installed skills")
    args = p.parse_args()
    if not args.codebase.is_dir():
        raise SystemExit(f"{args.codebase} isn't a folder")
    result = scan(args.codebase.resolve(), args.skills_dir)
    args.out.write_text(json.dumps(result, indent=1) + "\n")
    kinds = {}
    for f in result["files"]:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    print(f"{len(result['files'])} files ({', '.join(f'{n} {k}' for k, n in sorted(kinds.items(), key=lambda x: -x[1]))}); "
          f"{len(result['signals'])} base-skill signals, {len(result['settings'])} settings, "
          f"{len(result['resources'])} hard-coded names, {len(result['secrets'])} secrets → {args.out}")
    if result["hard_coded_tokens"]:
        print(f"Hard-coded tokens at {', '.join(result['hard_coded_tokens'])}: tell the user; never copy them.",
              file=sys.stderr)
    if result["deprecated"]:
        print(f"Deprecated products used: {', '.join(d['product'] for d in result['deprecated'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
