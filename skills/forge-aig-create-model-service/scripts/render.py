# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Write the YAML files the user fills in, from aig-model-services.json (forge-aig-create-model-service module 01).

    uv run <skill>/scripts/render.py --state <workdir>/aig-model-services.json

Writes into <workdir>/<config_dir>/ the file for each part in `parts`: model-services.yaml (model_services),
service-policies.yaml (service_policies), and permissions.yaml (grants). Each comes from the template of the same name
next to this script, with the target catalog and schema, the default evaluator, and the starter service's policies
filled in. Every other `<...>` is the user's to fill in, and the plan scripts refuse to run while one is left. A file
that's already there is the user's, so it's never replaced; it's listed under kept. To start one over, move it aside
and render again. Prints a JSON summary, with the placeholders each file still has.
"""

import argparse
import json
import re
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
TEMPLATES = {"model_services": "model-services.yaml", "service_policies": "service-policies.yaml",
             "grants": "permissions.yaml"}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DIR_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
FILLED = re.compile(r"<(catalog|schema|default_evaluator|starter_policies)>")  # every other <...> is the user's
PLACEHOLDER_RE = re.compile(r"<[^<>]+>")
DEFAULT_EVALUATOR = "system.ai.gpt-oss-20b"
STARTER_POLICIES = "[jailbreak_detection, pii_redaction]"  # defined in the service-policies.yaml template


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", required=True, type=Path, help="<workdir>/aig-model-services.json")
    return p.parse_args()


def load_state(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"{path} not found. Module 00 writes it; run preflight first.")
    state = json.loads(path.read_text())
    errors = [f"{key} is missing or empty" for key in ("profile", "catalog", "schema", "config_dir", "parts")
              if not state.get(key)]
    parts = state.get("parts") or []
    if not isinstance(parts, list) or set(parts) - set(TEMPLATES):
        errors.append(f"parts={parts!r}: any of {list(TEMPLATES)}")
    elif "service_policies" in parts and "model_services" not in parts:
        errors.append("service_policies needs model_services: policies are attached to the services in "
                      "model-services.yaml (list an existing service there to add policies to it)")
    for key in ("catalog", "schema"):
        if state.get(key) and not NAME_RE.match(str(state[key])):
            errors.append(f"{key}={state[key]!r}: letters, digits, '_' and '-'")
    if state.get("config_dir") and (not DIR_RE.match(str(state["config_dir"])) or state["config_dir"] in (".", "..")):
        errors.append(f"config_dir={state['config_dir']!r}: one folder name inside the working directory")
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    return state


def values(state: dict) -> dict[str, str]:
    return {"catalog": state["catalog"], "schema": state["schema"],
            "default_evaluator": state.get("default_evaluator") or DEFAULT_EVALUATOR,
            "starter_policies": STARTER_POLICIES if "service_policies" in state["parts"] else "[]"}


def placeholders(value) -> list[str]:
    if isinstance(value, dict):
        return [p for v in value.values() for p in placeholders(v)]
    if isinstance(value, list):
        return [p for v in value for p in placeholders(v)]
    return [value] if isinstance(value, str) and PLACEHOLDER_RE.search(value) else []


def render(name: str, state: dict) -> str:
    text = FILLED.sub(lambda m: values(state)[m.group(1)], (SCRIPTS / name).read_text())
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SystemExit(f"{name}: the rendered template isn't valid YAML: {e}") from None
    if not isinstance(parsed, dict):
        raise SystemExit(f"{name}: the rendered template's top level isn't a mapping")
    return text


def main() -> None:
    args = parse_args()
    state = load_state(args.state)
    folder = args.state.resolve().parent / state["config_dir"]
    summary = {"folder": str(folder), "written": [], "kept": [], "to_fill": {}}
    for part in state["parts"]:
        name = TEMPLATES[part]
        path = folder / name
        if path.exists():
            summary["kept"].append(str(path))  # the user's file
        else:
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(render(name, state))
            summary["written"].append(str(path))
        try:
            summary["to_fill"][name] = sorted(set(placeholders(yaml.safe_load(path.read_text()) or {})))
        except yaml.YAMLError as e:
            summary["to_fill"][name] = [f"not valid YAML: {e}"]
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
