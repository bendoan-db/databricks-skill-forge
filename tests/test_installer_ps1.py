"""Checks for install.ps1 with stand-in databricks, uv, and AI Dev Kit commands: no network, no workspace.

Runs under every PowerShell found: pwsh (PowerShell 7) anywhere, and powershell (Windows PowerShell 5.1) on
Windows. Skipped when neither is installed; .github/workflows/release.yml runs it on Linux, macOS, and Windows.
The stand-ins are Python scripts with sh and .cmd wrappers, so they work on every OS.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import SKILLS, SKILLS_DIR

ROOT = SKILLS_DIR.parent
INSTALLER = ROOT / "install.ps1"
HOSTS = [h for h in ("pwsh", "powershell") if shutil.which(h)]
pytestmark = pytest.mark.skipif(not HOSTS, reason="no PowerShell installed")
BASE_SKILLS = ["databricks-ai-functions", "databricks-apps-python", "databricks-core", "databricks-dabs", "databricks-jobs",
               "databricks-mlflow-evaluation", "databricks-serverless", "databricks-serverless-migration",
               "databricks-unity-catalog", "databricks-unity-gateway", "databricks-vector-search"]
MLFLOW = ["agent-evaluation", "mlflow-onboarding"]

# Stand-in AI Dev Kit install.ps1: records how it was called, then writes base skill folders the way
# `databricks aitools install --skills-only` does. The MLflow list is in the format install.ps1 parses.
FAKE_AIDK_INSTALL = """$script:MlflowSkills = @(
    "agent-evaluation", "mlflow-onboarding"
)
Set-Content -LiteralPath (Join-Path $env:FAKE_LOG 'aidk_install') -Value @((Get-Location).Path, $env:DEVKIT_SCOPE, ($args -join ' '))
$tools = ''; $skills = ''
for ($i = 0; $i -lt $args.Count; $i++) {
    if ($args[$i] -eq '--tools') { $tools = $args[$i + 1] }
    if ($args[$i] -eq '--skills') { $skills = $args[$i + 1] }
}
foreach ($tool in ($tools -split ',')) {
    $dir = switch ($tool) { 'claude' { '.claude' } 'codex' { '.agents' } default { '' } }
    if (-not $dir) { continue }
    foreach ($s in ($skills -split ',')) {
        $path = Join-Path (Join-Path (Join-Path (Get-Location).Path $dir) 'skills') $s
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $path 'SKILL.md') -Value "---`nname: $s`ndescription: x`nmetadata:`n  version: `"9.9.9`"`n---"
    }
}
exit 0
"""
FAKE_MCP_INSTALL = """Set-Content -LiteralPath (Join-Path $env:FAKE_LOG 'mcp_install') -Value @((Get-Location).Path, $env:DEVKIT_SCOPE, ($args -join ' '))
exit 0
"""
FAKE_DATABRICKS = """import sys
if sys.argv[1:2] == ["--version"]:
    print("Databricks CLI v1.20.0")
elif sys.argv[1:2] == ["aitools"]:
    print(open(sys.argv[0] + ".json").read())
"""
FAKE_UV = """import subprocess, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("uv 0.10.9")
elif args[:1] == ["run"]:
    args = args[2:] if args[1:2] == ["-q"] else args[1:]
    sys.exit(subprocess.run([sys.executable, "-B", *args]).returncode)
"""


def stand_in(bin_dir: Path, name: str, code: str) -> None:
    """name (sh) and name.cmd (Windows), both running name.py with this test's Python."""
    script = bin_dir / f"{name}.py"
    script.write_text(code)
    posix = bin_dir / name
    posix.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    posix.chmod(0o755)
    (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n')


@pytest.fixture
def env(tmp_path: Path) -> dict:
    bin_dir, log, home, project = tmp_path / "bin", tmp_path / "log", tmp_path / "home", tmp_path / "project"
    for folder in (bin_dir, log, home, project):
        folder.mkdir()
    stand_in(bin_dir, "databricks", FAKE_DATABRICKS)
    (bin_dir / "databricks.py.json").write_text(json.dumps({"release": "0.2.30", "skills": [
        *({"name": s, "experimental": False} for s in BASE_SKILLS),
        {"name": "spark-python-data-source", "experimental": True},
    ]}))
    stand_in(bin_dir, "uv", FAKE_UV)
    aidk = tmp_path / "ai-dev-kit"
    (aidk / "databricks-mcp-server").mkdir(parents=True)
    (aidk / "install.ps1").write_text(FAKE_AIDK_INSTALL)
    (aidk / "databricks-mcp-server" / "mcp_install.ps1").write_text(FAKE_MCP_INSTALL)
    (home / ".databrickscfg").write_text("[DEFAULT]\nhost = https://example.cloud.databricks.com\n")
    variables = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(home),
                 "USERPROFILE": str(home), "FAKE_LOG": str(log), "FORGE_AIDEVKIT_DIR": str(aidk),
                 "PYTHONDONTWRITEBYTECODE": "1"}
    variables.pop("DATABRICKS_CONFIG_PROFILE", None)
    variables.pop("DATABRICKS_CONFIG_FILE", None)
    return {"vars": variables, "log": log, "project": project, "tmp": tmp_path}


def run(env: dict, host: str, *args: str, installer: Path = INSTALLER, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run([host, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(installer), *args],
                            env=env["vars"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def forge_installed(env: dict, skills_dir: str) -> bool:
    return all((env["project"] / skills_dir / "skills" / name / "SKILL.md").is_file() for name in ["forge", *SKILLS])


@pytest.mark.parametrize("host", HOSTS)
def test_help_and_version(env: dict, host: str):
    help_text = run(env, host, "-Help").stdout
    for option in ("-Global", "-Dir", "-Tools", "-Profile", "-NoMcp", "-ForgeOnly", "-AiDevKitRef", "-DryRun", "-Yes"):
        assert option in help_text
    assert "development checkout" in run(env, host, "-Version").stdout


@pytest.mark.parametrize("host", HOSTS)
def test_installs_base_skills_by_name_then_forge(env: dict, host: str):
    run(env, host, "-Dir", str(env["project"]), "-Tools", "claude,codex", "-Yes")
    cwd, scope, args = (env["log"] / "aidk_install").read_text().splitlines()
    assert Path(cwd).resolve() == env["project"].resolve() and scope == "project"
    assert "--tools claude,codex" in args and "--experimental false" in args and "--force" in args and "--silent" in args
    assert "--global" not in args and "  " not in args  # no empty arguments
    requested = args.split("--skills ", 1)[1].split(" ", 1)[0].split(",")
    assert sorted(requested) == sorted(BASE_SKILLS + MLFLOW)
    _, mcp_scope, mcp_args = (env["log"] / "mcp_install").read_text().splitlines()
    assert mcp_scope == "project" and "-Tools claude,codex -Profile_ DEFAULT -Silent" in mcp_args
    assert forge_installed(env, ".claude") and forge_installed(env, ".agents")


@pytest.mark.parametrize("host", HOSTS)
def test_bash_style_flags_and_dry_run(env: dict, host: str):
    result = run(env, host, "--dir", str(env["project"]), "--tools", "claude", "--dry-run")
    assert "11 stable skills" in result.stdout and "Dry run" in result.stdout
    assert not list(env["project"].iterdir()) and not list(env["log"].iterdir())


@pytest.mark.parametrize("host", HOSTS)
def test_needs_yes_without_a_terminal(env: dict, host: str):
    result = run(env, host, "-Dir", str(env["project"]), check=False)
    assert result.returncode == 1 and "-Yes" in result.stdout + result.stderr
    assert not list(env["log"].iterdir())


@pytest.mark.parametrize("host", HOSTS)
def test_release_installer_verifies_its_download(env: dict, host: str):
    spec = importlib.util.spec_from_file_location("build_release", ROOT / "tools" / "build_release.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tag = f"v{(ROOT / 'VERSION').read_text().strip()}"
    dist, mirror = env["tmp"] / "dist", env["tmp"] / "release-mirror"
    tool.build(tag, dist)
    (mirror / tag).mkdir(parents=True)
    archive = Path(shutil.copy(dist / f"forge-skills-{tag}.tar.gz", mirror / tag))
    (env["tmp"] / "downloaded").mkdir()
    installer = Path(shutil.copy(dist / "install.ps1", env["tmp"] / "downloaded" / "install.ps1"))
    env["vars"]["FORGE_RELEASE_URL"] = mirror.as_uri()
    result = run(env, host, "-Dir", str(env["project"]), "-Yes", installer=installer)
    assert "checksum verified" in result.stdout and forge_installed(env, ".claude")
    with archive.open("ab") as stream:
        stream.write(b"tampered")
    shutil.rmtree(env["project"] / ".claude")
    result = run(env, host, "-Dir", str(env["project"]), "-Yes", installer=installer, check=False)
    assert result.returncode == 1 and "doesn't match its checksum" in result.stdout + result.stderr
    assert not list(env["project"].iterdir())
