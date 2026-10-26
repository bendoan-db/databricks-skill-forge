"""Checks for install.sh with stand-in databricks, uv, git, and AI Dev Kit: no network, no workspace.

The installer runs under /bin/bash when it exists, so these also cover macOS's bash 3.2. Downloads come from
file:// mirrors of what a release and GitHub serve. A git stand-in fails the test if anything calls it.
"""

import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from conftest import SKILLS, SKILLS_DIR

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="install.sh is for macOS and Linux; install.ps1 is tested on Windows")
INSTALLER = SKILLS_DIR.parent / "install.sh"
BASH = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")
BASE_SKILLS = ["databricks-ai-functions", "databricks-apps-python", "databricks-core", "databricks-dabs", "databricks-jobs",
               "databricks-mlflow-evaluation", "databricks-serverless", "databricks-serverless-migration",
               "databricks-unity-catalog", "databricks-unity-gateway", "databricks-vector-search", "databricks-genie-agents"]
MLFLOW = ["agent-evaluation", "mlflow-onboarding"]

# Stand-in AI Dev Kit install.sh: records how it was called, then writes base skill folders into each tool's
# skills dir the way `databricks aitools install --skills-only` does.
FAKE_AIDK_INSTALL = f"""#!/usr/bin/env bash
MLFLOW_SKILLS="{' '.join(MLFLOW)}"
printf '%s\\n' "$PWD" "$DEVKIT_SCOPE" "$*" > "$FAKE_LOG/aidk_install"
while [ $# -gt 0 ]; do
  case $1 in --tools) tools=$2; shift 2 ;; --skills) skills=$2; shift 2 ;; *) shift ;; esac
done
for tool in $(echo "$tools" | tr ',' ' '); do
  case $tool in claude) dir=.claude/skills ;; codex) dir=.agents/skills ;; *) continue ;; esac
  for s in $(echo "$skills" | tr ',' ' '); do
    mkdir -p "$dir/$s" && printf -- '---\\nname: %s\\ndescription: x\\nmetadata:\\n  version: "9.9.9"\\n---\\n' "$s" > "$dir/$s/SKILL.md"
  done
done
"""
FAKE_MCP_INSTALL = """#!/usr/bin/env bash
printf '%s\\n' "$PWD" "$DEVKIT_SCOPE" "$*" > "$FAKE_LOG/mcp_install"
"""
PINNED = "v0.2.0"


def write_exe(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def env(tmp_path: Path) -> dict:
    bin_dir, log = tmp_path / "bin", tmp_path / "log"
    log.mkdir()
    listing = {"release": "0.2.30", "skills": [
        *({"name": s, "latest_version": "0.1.0", "experimental": False, "installed": {}} for s in BASE_SKILLS),
        {"name": "spark-python-data-source", "latest_version": "0.1.0", "experimental": True, "installed": {}},
    ], "summary": {"name": "not a skill"}, "agents": [{"name": "claude-code"}]}
    (tmp_path / "aitools.json").write_text(json.dumps(listing, indent=2))
    write_exe(bin_dir / "databricks", f"""#!/usr/bin/env bash
case "$1" in
  --version) echo "Databricks CLI v1.20.0" ;;
  aitools) cat "{tmp_path / 'aitools.json'}" ;;
esac
""")
    # uv run -q <script> ...: run the script with this test's Python, which has pyyaml
    write_exe(bin_dir / "uv", f"""#!/usr/bin/env bash
case "$1" in
  --version) echo "uv 0.10.9" ;;
  run) shift; [ "$1" = -q ] && shift; exec "{sys.executable}" -B "$@" ;;
esac
""")
    write_exe(bin_dir / "git", 'echo "$*" >> "$FAKE_LOG/git"; exit 97\n')
    aidk = tmp_path / "ai-dev-kit"
    write_exe(aidk / "install.sh", FAKE_AIDK_INSTALL)
    write_exe(aidk / "databricks-mcp-server" / "mcp_install.sh", FAKE_MCP_INSTALL)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".databrickscfg").write_text("[DEFAULT]\nhost = https://example.cloud.databricks.com\n")
    project = tmp_path / "project"
    project.mkdir()
    return {
        "vars": {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home), "FAKE_LOG": str(log),
                 "FORGE_AIDEVKIT_DIR": str(aidk), "PYTHONDONTWRITEBYTECODE": "1"},
        "log": log, "project": project, "home": home, "listing": tmp_path / "aitools.json", "tmp": tmp_path,
    }


def run(env: dict, *args: str, check: bool = True, installer: Path = INSTALLER) -> subprocess.CompletedProcess:
    result = subprocess.run([BASH, str(installer), *args], env=env["vars"], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, start_new_session=True)  # no controlling terminal
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    assert not (env["log"] / "git").exists(), "the installer called git"
    return result


def tarball(files: dict[str, str], top: str, out: Path) -> str:
    """A .tar.gz with one top-level folder, like GitHub's source archives; returns its SHA-256."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw.getvalue())
    return hashlib.sha256(raw.getvalue()).hexdigest()


def serve_ai_dev_kit(env: dict, mcp_exit: int = 0) -> str:
    """Mirror AI Dev Kit's source archive, and stop using the local copy; returns the archive's SHA-256."""
    mirror = env["tmp"] / "aidk-mirror"
    sha = tarball({"install.sh": FAKE_AIDK_INSTALL,
                   "databricks-mcp-server/mcp_install.sh": FAKE_MCP_INSTALL + f"exit {mcp_exit}\n"},
                  "ai-dev-kit-0.2.0", mirror / PINNED)
    del env["vars"]["FORGE_AIDEVKIT_DIR"]
    env["vars"].update(FORGE_AIDEVKIT_ARCHIVE_URL=f"file://{mirror}", FORGE_AIDEVKIT_SHA256=sha,
                       FORGE_HOME=str(env["tmp"] / "forge-home"))
    return sha


def release(env: dict) -> Path:
    """Build a release into a file:// mirror; returns the stamped install.sh, outside any checkout."""
    spec = importlib.util.spec_from_file_location("build_release", SKILLS_DIR.parent / "tools" / "build_release.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tag = f"v{(SKILLS_DIR.parent / 'VERSION').read_text().strip()}"
    dist, mirror = env["tmp"] / "dist", env["tmp"] / "release-mirror"
    tool.build(tag, dist)
    (mirror / tag).mkdir(parents=True)
    shutil.copy(dist / f"forge-skills-{tag}.tar.gz", mirror / tag)
    env["vars"]["FORGE_RELEASE_URL"] = f"file://{mirror}"
    env["archive"] = mirror / tag / f"forge-skills-{tag}.tar.gz"
    (env["tmp"] / "downloaded").mkdir()
    return shutil.copy(dist / "install.sh", env["tmp"] / "downloaded" / "install.sh")


def test_installs_base_skills_by_name_then_forge(env: dict):
    run(env, "--dir", str(env["project"]), "--tools", "claude,codex", "-p", "DEFAULT", "--yes")
    cwd, scope, args = (env["log"] / "aidk_install").read_text().splitlines()
    assert cwd == str(env["project"]) and scope == "project"
    assert "--tools claude,codex" in args and "--experimental false" in args and "--force" in args and "--silent" in args
    assert "--global" not in args
    requested = args.split("--skills ", 1)[1].split(" ", 1)[0].split(",")
    assert sorted(requested) == sorted(BASE_SKILLS + MLFLOW)  # every stable skill, plus MLflow; no experimental ones
    mcp_cwd, mcp_scope, mcp_args = (env["log"] / "mcp_install").read_text().splitlines()
    assert mcp_cwd == str(env["project"]) and mcp_scope == "project" and "--tools claude,codex -p DEFAULT" in mcp_args
    for skills_dir in (".claude/skills", ".agents/skills"):
        for name in ["forge", *SKILLS]:
            installed = env["project"] / skills_dir / name
            assert (installed / "SKILL.md").read_text() == (SKILLS_DIR / name / "SKILL.md").read_text()
            assert not list(installed.rglob("__pycache__"))


def test_global_scope(env: dict):
    run(env, "--global", "--yes")
    _, scope, args = (env["log"] / "aidk_install").read_text().splitlines()
    assert scope == "global" and args.endswith("--global")
    assert (env["home"] / ".claude" / "skills" / "forge" / "SKILL.md").is_file()


def test_no_mcp_and_forge_only(env: dict):
    run(env, "--dir", str(env["project"]), "--no-mcp", "--yes")
    assert not (env["log"] / "mcp_install").exists()
    (env["log"] / "aidk_install").unlink()
    run(env, "--dir", str(env["project"]), "--forge-only", "--yes")  # updates Forge next to the existing base skills
    assert not (env["log"] / "aidk_install").exists()


def test_forge_only_needs_base_skills(env: dict):
    result = run(env, "--dir", str(env["project"]), "--forge-only", "--yes", check=False)
    assert result.returncode == 1 and "No skills directory had the base skills" in result.stderr


def test_removes_only_forge_made_legacy_copies(env: dict):
    skills_dir = env["project"] / ".claude" / "skills"
    (skills_dir / "doc-qa-agent").mkdir(parents=True)
    (skills_dir / "doc-qa-agent" / "SKILL.md").write_text("---\nname: doc-qa-agent\nmetadata:\n  forge_kind: use-case\n---\n")
    (skills_dir / "structured-doc-extraction").mkdir()
    (skills_dir / "structured-doc-extraction" / "SKILL.md").write_text("---\nname: structured-doc-extraction\n---\n")  # not Forge's
    run(env, "--dir", str(env["project"]), "--yes")
    assert not (skills_dir / "doc-qa-agent").exists()
    assert (skills_dir / "structured-doc-extraction").exists()


def test_dry_run_changes_nothing(env: dict):
    result = run(env, "--dir", str(env["project"]), "--tools", "claude,codex", "--dry-run")
    assert "12 stable skills from databricks-agent-skills 0.2.30" in result.stdout and "Dry run" in result.stdout
    assert not list(env["project"].iterdir()) and not list(env["log"].iterdir())


def test_needs_confirmation_without_a_terminal(env: dict):
    result = run(env, "--dir", str(env["project"]), check=False)
    assert result.returncode == 1 and "--yes" in result.stderr
    assert not list(env["log"].iterdir())


def test_stops_when_a_required_base_skill_is_missing(env: dict):
    listing = json.loads(env["listing"].read_text())
    listing["skills"] = [s for s in listing["skills"] if s["name"] != "databricks-vector-search"]
    env["listing"].write_text(json.dumps(listing, indent=2))
    result = run(env, "--dir", str(env["project"]), "--yes", check=False)
    assert result.returncode == 1 and "databricks-vector-search" in result.stderr
    assert not list(env["log"].iterdir())


def test_reports_use_case_skills_missing_base_skills(env: dict):
    run(env, "--dir", str(env["project"]), "--yes")
    shutil.rmtree(env["project"] / ".claude" / "skills" / "databricks-vector-search")
    result = run(env, "--dir", str(env["project"]), "--forge-only", "--yes", check=False)
    assert result.returncode == 1 and "databricks-vector-search" in result.stderr


def test_rejects_unknown_tools(env: dict):
    result = run(env, "--tools", "claude,emacs", "--dry-run", check=False)
    assert result.returncode == 1 and "Unknown tool 'emacs'" in result.stderr


def test_help_lists_every_option():
    result = subprocess.run([BASH, str(INSTALLER), "--help"], capture_output=True, text=True, check=True,
                            env={**os.environ, "PATH": "/usr/bin:/bin"})
    for option in ("--global", "--dir", "--tools", "--profile", "--no-mcp", "--forge-only", "--ai-dev-kit-ref", "--dry-run", "--yes"):
        assert option in result.stdout


def forge_installed(env: dict, skills_dir: str = ".claude/skills") -> bool:
    return all((env["project"] / skills_dir / name / "SKILL.md").is_file() for name in ["forge", *SKILLS])


def test_release_installer_needs_no_checkout_or_git(env: dict):
    installer = release(env)
    result = run(env, "--dir", str(env["project"]), "--yes", installer=installer)
    assert "checksum verified" in result.stdout and forge_installed(env)


def test_release_installer_stops_on_a_bad_checksum(env: dict):
    installer = release(env)
    with env["archive"].open("ab") as archive:
        archive.write(b"tampered")
    result = run(env, "--dir", str(env["project"]), "--yes", installer=installer, check=False)
    assert result.returncode == 1 and "doesn't match its checksum" in result.stderr
    assert not list(env["project"].iterdir()) and not (env["log"] / "aidk_install").exists()


def test_refuses_plain_http(env: dict):
    installer = release(env)
    env["vars"]["FORGE_RELEASE_URL"] = "http://example.com/releases"
    result = run(env, "--dir", str(env["project"]), "--dry-run", installer=installer, check=False)
    assert result.returncode == 1 and "use an https:// URL" in result.stderr


def test_unstamped_installer_outside_a_checkout_installs_main_unverified(env: dict):
    mirror = env["tmp"] / "source-mirror"
    files = {f"skills/{p.relative_to(SKILLS_DIR).as_posix()}": p.read_text()
             for p in SKILLS_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    for ref in ("main", "dev"):
        tarball(files, f"databricks-skill-forge-{ref}", mirror / ref)
    env["vars"]["FORGE_ARCHIVE_URL"] = f"file://{mirror}"
    (env["tmp"] / "downloaded").mkdir()
    installer = shutil.copy(INSTALLER, env["tmp"] / "downloaded" / "install.sh")
    result = run(env, "--dir", str(env["project"]), "--yes", installer=installer)
    assert "main branch, unverified" in result.stderr and forge_installed(env)
    result = run(env, "--dir", str(env["project"]), "--forge-ref", "dev", "--dry-run", installer=installer)
    assert "the dev source, unverified" in result.stdout


def test_downloads_ai_dev_kit_once_and_replaces_an_older_copy(env: dict):
    sha = serve_ai_dev_kit(env)
    copy = env["tmp"] / "forge-home" / "ai-dev-kit"
    (copy / ".git").mkdir(parents=True)  # what the git-based installer left behind
    (copy / "old-marker").write_text("old")
    run(env, "--dir", str(env["project"]), "--yes")
    assert (copy / "install.sh").is_file() and not (copy / "old-marker").exists()
    assert (copy / ".forge-source").read_text().strip() == f"{PINNED} {sha}"
    assert not (copy.parent / "ai-dev-kit.old").exists() and not (copy.parent / "ai-dev-kit.new").exists()
    assert (env["log"] / "mcp_install").is_file() and forge_installed(env)
    result = run(env, "--dir", str(env["project"]), "--yes")
    assert f"Already have {PINNED}" in result.stdout


def test_restores_the_previous_copy_when_the_mcp_install_fails(env: dict):
    serve_ai_dev_kit(env, mcp_exit=1)
    copy = env["tmp"] / "forge-home" / "ai-dev-kit"
    copy.mkdir(parents=True)
    (copy / "old-marker").write_text("the copy the MCP server runs from")
    result = run(env, "--dir", str(env["project"]), "--yes")
    assert "put back" in result.stderr and (copy / "old-marker").is_file()
    assert not (copy.parent / "ai-dev-kit.old").exists() and forge_installed(env)


def test_bad_ai_dev_kit_checksum_installs_nothing(env: dict):
    serve_ai_dev_kit(env)
    env["vars"]["FORGE_AIDEVKIT_SHA256"] = "0" * 64
    result = run(env, "--dir", str(env["project"]), "--yes", check=False)
    assert result.returncode == 1 and "doesn't match its checksum" in result.stderr
    assert not (env["tmp"] / "forge-home").exists() and not list(env["project"].iterdir())


def test_no_mcp_uses_a_throwaway_ai_dev_kit(env: dict):
    serve_ai_dev_kit(env)
    run(env, "--dir", str(env["project"]), "--no-mcp", "--yes")
    assert not (env["tmp"] / "forge-home").exists() and forge_installed(env)


def test_other_ai_dev_kit_refs_are_unverified(env: dict):
    result = run(env, "--dir", str(env["project"]), "--ai-dev-kit-ref", "main", "--dry-run")
    assert "the copy in" in result.stdout  # FORGE_AIDEVKIT_DIR wins
    del env["vars"]["FORGE_AIDEVKIT_DIR"]
    result = run(env, "--dir", str(env["project"]), "--ai-dev-kit-ref", "main", "--dry-run")
    assert f"main, unverified (only {PINNED} has a pinned checksum)" in result.stdout


def test_version(env: dict):
    assert "development checkout" in run(env, "--version").stdout
    assert f"Forge installer v{(SKILLS_DIR.parent / 'VERSION').read_text().strip()}" in run(env, "--version", installer=release(env)).stdout

