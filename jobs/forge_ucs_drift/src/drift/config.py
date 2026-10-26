"""Settings for one run of forge_ucs_drift, from the job's task parameters (or a local dev run's flags)."""

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path

STEPS = ("detect", "analyze", "propose", "publish")
MODEL_RE = re.compile(r"^system\.ai\.[a-z0-9-]+$")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class Settings:
    run_id: str
    catalog: str = "fins_skill_forge_catalog"
    schema: str = "forge_drift"
    volume: str = "drift"
    dry_run: bool = True
    force: bool = False
    skills: list[str] = field(default_factory=list)  # empty: every Forge skill
    upstream_repo: str = "databricks/databricks-agent-skills"
    forge_repo: str = "bendoan-db/databricks-skill-forge"
    forge_branch: str = "main"
    assess_model: str = "system.ai.claude-sonnet-5-5"
    edit_model: str = "system.ai.claude-opus-5-5"
    secret_scope: str = "forge"
    secret_key: str = "git-pat"
    local_dir: str = ""  # development: files and JSON Lines tables here, instead of the volume and Delta
    no_llm: bool = False  # development: skip the LLM steps

    def __post_init__(self) -> None:
        for name in ("catalog", "schema", "volume"):
            if not NAME_RE.match(getattr(self, name)):
                raise SystemExit(f"--{name}={getattr(self, name)!r}: use letters, digits, '_' or '-'")
        for name in ("assess_model", "edit_model"):
            if not MODEL_RE.match(getattr(self, name)):
                raise SystemExit(f"--{name.replace('_', '-')}={getattr(self, name)!r}: use a system.ai model name")

    @property
    def root(self) -> Path:
        """Where upstream trees, Forge trees, and run artifacts live."""
        return Path(self.local_dir) if self.local_dir else Path(f"/Volumes/{self.catalog}/{self.schema}/{self.volume}")

    def table(self, name: str) -> str:
        return f"`{self.catalog}`.`{self.schema}`.`{name}`"


def parse_args(argv: list[str] | None = None) -> tuple[str, Settings]:
    p = argparse.ArgumentParser(description="forge_ucs_drift: propose Forge skill updates for upstream base-skill changes")
    p.add_argument("step", choices=[*STEPS, "all"], help="'all' runs every step in order (local development)")
    p.add_argument("--run-id", default="local")
    p.add_argument("--catalog", default=Settings.catalog)
    p.add_argument("--schema", default=Settings.schema)
    p.add_argument("--volume", default=Settings.volume)
    p.add_argument("--dry-run", choices=["true", "false"], default="true", help="true: no GitHub writes")
    p.add_argument("--force", choices=["true", "false"], default="false", help="Analyze even if nothing changed")
    p.add_argument("--skills", default="", help="Comma-separated Forge skills to analyze (default: all)")
    p.add_argument("--forge-repo", default=Settings.forge_repo)
    p.add_argument("--forge-branch", default=Settings.forge_branch)
    p.add_argument("--assess-model", default=Settings.assess_model)
    p.add_argument("--edit-model", default=Settings.edit_model)
    p.add_argument("--secret-scope", default=Settings.secret_scope)
    p.add_argument("--secret-key", default=Settings.secret_key)
    p.add_argument("--local-dir", default="", help="Development: keep state in this folder instead of the workspace")
    p.add_argument("--no-llm", action="store_true", help="Development: skip the LLM steps")
    a = p.parse_args(argv)
    settings = Settings(
        run_id=a.run_id, catalog=a.catalog, schema=a.schema, volume=a.volume, dry_run=a.dry_run == "true",
        force=a.force == "true", skills=[s for s in a.skills.split(",") if s.strip()], forge_repo=a.forge_repo,
        forge_branch=a.forge_branch, assess_model=a.assess_model, edit_model=a.edit_model,
        secret_scope=a.secret_scope, secret_key=a.secret_key, local_dir=a.local_dir, no_llm=a.no_llm,
    )
    return a.step, settings
