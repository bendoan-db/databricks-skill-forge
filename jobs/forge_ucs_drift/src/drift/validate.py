"""Check a candidate Forge repo with its own checks, in a separate process with a minimal environment.

The candidate can include code the model wrote, so the checks run as a subprocess that gets no Databricks or
GitHub credentials in its environment, and this step never reads the GitHub token (only publish does). Test
failures count only when they're new: a test that also fails on unchanged main isn't the proposal's fault.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

TIMEOUT = 1800
TEST_ARGS = ["-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]


@dataclass
class Result:
    passed: bool
    failures: list[str] = field(default_factory=list)
    log: str = ""


def clean_env(home: Path) -> dict[str, str]:
    """PATH and locale only: no tokens, no Databricks host or auth, no proxy settings."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "TMPDIR": str(home),
            "LANG": "C.UTF-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def run(cmd: list[str], cwd: Path, home: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=cwd, env=clean_env(home), capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 124, e.stdout or "", f"timed out after {TIMEOUT}s")


def failed_tests(repo: Path, home: Path) -> tuple[set[str], str]:
    """Ids of the tests that fail in repo, and the tail of the pytest output."""
    junit = home / f"junit-{repo.name}.xml"
    result = run([sys.executable, "-B", *TEST_ARGS, f"--junitxml={junit}"], repo, home)
    failed = set()
    if junit.is_file():
        for case in ET.parse(junit).iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                failed.add(f"{case.get('classname')}::{case.get('name')}")
    elif result.returncode != 0:
        failed.add(f"pytest exited {result.returncode} without a report")
    return failed, (result.stdout + result.stderr)[-4000:]


def skills_dir_with(upstream_skills: Path, repo: Path) -> Path:
    """A folder holding the newer release's base skills next to the candidate's Forge skills, the way they're
    installed, so check_base_skills.py checks the candidate against what users will have."""
    folder = Path(tempfile.mkdtemp(prefix="forge-skills-"))
    for source in [*upstream_skills.iterdir(), *(repo / "skills").iterdir()]:
        if source.is_dir():
            (folder / source.name).symlink_to(source.resolve(), target_is_directory=True)
    return folder


def check(repo: Path, skill: str, upstream_skills: Path, baseline_failures: set[str]) -> Result:
    home = Path(tempfile.mkdtemp(prefix="forge-validate-"))
    failures, log = [], []
    try:
        candidate_failed, tail = failed_tests(repo, home)
        if new := sorted(candidate_failed - baseline_failures):
            failures += [f"test fails: {t}" for t in new]
            log.append(tail)
        catalog = run([sys.executable, "-B", "tools/build_forge_catalog.py", "--check"], repo, home)
        if catalog.returncode != 0:
            failures.append(f"forge catalog: {(catalog.stderr or catalog.stdout).strip()[-300:]}")
        if skill != "forge":
            installed = skills_dir_with(upstream_skills, repo)
            base = run([sys.executable, "-B", f"skills/{skill}/scripts/check_base_skills.py", "--skills-dir", str(installed)],
                       repo, home)
            shutil.rmtree(installed, ignore_errors=True)
            if base.returncode != 0:
                failures.append(f"check_base_skills.py against the new release: {base.stderr.strip()[-500:]}")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return Result(not failures, failures, "\n".join(log))


def baseline(repo: Path) -> set[str]:
    """Tests that already fail on unchanged main, in this environment."""
    home = Path(tempfile.mkdtemp(prefix="forge-baseline-"))
    try:
        return failed_tests(repo, home)[0]
    finally:
        shutil.rmtree(home, ignore_errors=True)
