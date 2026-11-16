# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Write forge-structured-doc-extraction's scripts/review-app/scaffold.yaml from an AppKit scaffold (maintainers).

    uv run tools/vendor_review_app_scaffold.py --version v0.84.0 --profile <profile>   # runs `databricks apps init`
    uv run tools/vendor_review_app_scaffold.py --version v0.84.0 --from <scaffolded app folder>

In a notebook (runtime workspace) the skill can't run `databricks apps init`, so render.py writes the AppKit project
from scaffold.yaml instead, and Databricks Apps installs and builds it on deploy. This makes scaffold.yaml from what
`databricks apps init --features lakebase,files --package-manager npm --skip-install` writes at an AppKit version: the
files a build and the deployed app need, less what only local development uses (.env, lint, tests, prettier, the
lockfile, the PNG icons, the sample pages and routes) and the three files render.py copies from scripts/review-app
(server.ts, review-routes.ts, App.tsx). The app's name becomes {{app_name}}, and the bundle's target variables
{{volume}}, {{branch}}, and {{database}}, which render.py fills in. The CLI needs a profile only for the workspace
host it writes into databricks.yml, which this drops. Nothing is installed or run.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / "skills" / "forge-structured-doc-extraction" / "scripts" / "review-app" / "scaffold.yaml"
SCAFFOLD_NAME = "forge-review-scaffold"
KEEP = [  # everything a build (npm install, npm run build) and the running app read
    ".gitignore", ".npmrc", "app.yaml", "appkit.plugins.json", "databricks.yml", "package.json",
    "tsconfig.json", "tsconfig.client.json", "tsconfig.server.json", "tsconfig.shared.json", "tsdown.server.config.ts",
    "client/index.html", "client/postcss.config.js", "client/tailwind.config.ts", "client/vite.config.ts",
    "client/public/favicon.svg", "client/src/ErrorBoundary.tsx", "client/src/index.css", "client/src/lib/utils.ts",
    "client/src/main.tsx", "client/src/vite-env.d.ts",
]
TARGETS = """
targets:
  default:
    default: true

    variables:
      files_path: "{{volume}}"
      postgres_branch: "{{branch}}"
      postgres_database: "{{database}}"
"""
HEADER = """\
# The AppKit project of forge-structured-doc-extraction's review app, for a notebook (runtime workspace), where
# `databricks apps init` can't run: render.py writes these files into <workdir>/<review.app_name>/ once, and never
# replaces them, then copies server.ts, review-routes.ts, and App.tsx over them as it does locally. Databricks Apps
# runs `npm install` and `npm run build` when the app deploys. Don't edit by hand: regenerate with
# `uv run tools/vendor_review_app_scaffold.py --version <tag>` (AppKit, Apache-2.0: github.com/databricks/appkit).
# render.py fills {{app_name}}, {{volume}}, {{branch}}, and {{database}}.
"""


def scaffold(version: str, profile: str, out: Path) -> Path:
    """Run `databricks apps init` into out, without installing; returns the app folder."""
    sets = ["lakebase.postgres.project=projects/forge-scaffold",
            "lakebase.postgres.branch=projects/forge-scaffold/branches/production",
            "lakebase.postgres.database=projects/forge-scaffold/branches/production/databases/databricks-postgres",
            "files.files.path=forge.scaffold.files"]
    subprocess.run(["databricks", "apps", "init", "--name", SCAFFOLD_NAME, "--version", version,
                    "--features", "lakebase,files", "--output-dir", str(out), "--run", "none", "--skip-install",
                    "--package-manager", "npm", *[arg for s in sets for arg in ("--set", s)], "--profile", profile],
                   check=True)
    return out / SCAFFOLD_NAME


def placeholders(path: str, text: str, name: str) -> str:
    text = text.replace(name, "{{app_name}}")
    if path == "client/index.html":  # no PNG icons or web manifest: only the SVG icon is vendored
        text = "".join(line for line in text.splitlines(keepends=True)
                       if not re.search(r'href="/[^"]+\.(png|webmanifest)"', line))
    if path == "databricks.yml":  # the target: no workspace host (the CLI's profile decides), and render.py's values
        if "\ntargets:\n" not in text:
            sys.exit("databricks.yml has no targets section: the scaffold changed, so update placeholders()")
        text = text.split("\ntargets:\n")[0] + TARGETS
    return text


class Literal(str):
    pass


def literal(dumper: yaml.SafeDumper, value: Literal):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--version", required=True, help="The AppKit tag, such as v0.84.0")
    p.add_argument("--profile", default="", help="A CLI profile, for `databricks apps init`")
    p.add_argument("--from", dest="source", type=Path, help="An app folder `databricks apps init` already wrote")
    args = p.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        folder = args.source or scaffold(args.version, args.profile, Path(tmp))
        name = __import__("json").loads((folder / "package.json").read_text())["name"]
        files = {}
        for path in KEEP:
            source = folder / path
            if not source.is_file():
                sys.exit(f"{source} is missing: the scaffold changed, so update KEEP")
            files[path] = Literal(placeholders(path, source.read_text(), name))
    appkit = re.search(r'"@databricks/appkit": "([^"]+)"', files["package.json"]).group(1)
    if f"v{appkit}" != args.version:
        sys.exit(f"The scaffold pins AppKit {appkit}, not {args.version}")
    yaml.SafeDumper.add_representer(Literal, literal)
    body = yaml.safe_dump({"appkit_version": appkit, "files": files}, sort_keys=False, width=1000, allow_unicode=True)
    TARGET.write_text(HEADER + body)
    print(f"wrote {TARGET} ({len(files)} files, AppKit {appkit})")


if __name__ == "__main__":
    main()
