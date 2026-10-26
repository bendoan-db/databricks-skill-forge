"""Checks for tools/build_release.py, and for the facts install.sh and install.ps1 must agree on."""

import hashlib
import importlib.util
import io
import re
import shutil
import tarfile
from pathlib import Path

import pytest
from conftest import SKILLS_DIR

ROOT = SKILLS_DIR.parent


def load_tool():
    spec = importlib.util.spec_from_file_location("build_release", ROOT / "tools" / "build_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = load_tool()
TAG = f"v{(ROOT / 'VERSION').read_text().strip()}"


def members(archive: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return {m.name: m for m in tar.getmembers()}


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """A minimal copy of the repo that tests can add caches and executables to."""
    root = tmp_path / "repo"
    shutil.copytree(SKILLS_DIR, root / "skills")
    for name in ("VERSION", *tool.STAMPS):
        shutil.copy(ROOT / name, root / name)
    return root


def test_builds_are_identical(tmp_path: Path):
    first, second = tool.build(TAG, tmp_path / "a"), tool.build(TAG, tmp_path / "b")
    assert first == second
    assert (tmp_path / "a" / "SHA256SUMS").read_bytes() == (tmp_path / "b" / "SHA256SUMS").read_bytes()


def test_archive_holds_the_skills_and_nothing_else(repo_copy: Path, tmp_path: Path):
    cache = repo_copy / "skills" / "forge" / "__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_bytes(b"cache")
    (repo_copy / "skills" / ".DS_Store").write_bytes(b"finder")
    script = repo_copy / "skills" / "forge-structured-doc-extraction" / "scripts" / "install_tesseract.sh"
    script.chmod(0o755)
    tool.build(TAG, tmp_path / "dist", root=repo_copy)
    entries = members((tmp_path / "dist" / f"forge-skills-{TAG}.tar.gz").read_bytes())
    expected = {"forge-skills"} | {f"forge-skills/{p.relative_to(SKILLS_DIR).as_posix()}" for p in tool.skill_files(SKILLS_DIR)}
    assert set(entries) == expected
    assert not [name for name in entries if "__pycache__" in name or name.endswith(".DS_Store")]
    assert entries["forge-skills/forge-structured-doc-extraction/scripts/install_tesseract.sh"].mode == 0o755
    assert entries["forge-skills/forge/SKILL.md"].mode == 0o644
    assert {(m.uid, m.gid, m.uname, m.gname, m.mtime) for m in entries.values()} == {(0, 0, "", "", tool.MTIME)}


def test_installers_are_stamped_with_the_archive_checksum(tmp_path: Path):
    sums = tool.build(TAG, tmp_path)
    archive_sha = sums[f"forge-skills-{TAG}.tar.gz"]
    assert archive_sha == hashlib.sha256((tmp_path / f"forge-skills-{TAG}.tar.gz").read_bytes()).hexdigest()
    sh = (tmp_path / "install.sh").read_text()
    assert f'FORGE_VERSION="{TAG}"' in sh and f'FORGE_SKILLS_SHA256="{archive_sha}"' in sh
    ps1 = (tmp_path / "install.ps1").read_text()
    assert f"$ForgeVersion = '{TAG}'" in ps1 and f"$ForgeSkillsSha256 = '{archive_sha}'" in ps1
    notebook = (tmp_path / "install_genie_code.py").read_text()
    assert f'FORGE_VERSION = "{TAG}"' in notebook and f'FORGE_SKILLS_SHA256 = "{archive_sha}"' in notebook
    for line in (tmp_path / "SHA256SUMS").read_text().splitlines():
        sha, name = line.split("  ")
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == sha
    with pytest.raises(SystemExit):
        tool.stamp(sh, "install.sh", TAG, archive_sha)  # already stamped: nothing left to match


def test_checkout_installers_are_unstamped():
    assert re.search(r'(?m)^FORGE_VERSION=""', (ROOT / "install.sh").read_text())
    assert re.search(r"(?m)^\$ForgeVersion = ''", (ROOT / "install.ps1").read_text())
    assert re.search(r'(?m)^FORGE_VERSION = ""', (ROOT / "install_genie_code.py").read_text())


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1", "v01.2.3", "v0.1.0-rc1", "v9.9.9"])
def test_rejects_bad_or_mismatched_tags(tag: str):
    with pytest.raises(SystemExit):
        tool.check_tag(tag)


def test_release_notes_list_every_skill(tmp_path: Path):
    tool.build(TAG, tmp_path)
    notes = (tmp_path / "RELEASE_NOTES.md").read_text()
    for asset in ("install.sh", "install.ps1", "install_genie_code.py"):
        assert f"/releases/download/{TAG}/{asset}" in notes
    for skill in (p.name for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()):
        assert f"| `{skill}` |" in notes


def test_installers_agree():
    sh, ps1 = (ROOT / "install.sh").read_text(), (ROOT / "install.ps1").read_text()

    def sh_value(name: str) -> str:
        return re.search(rf'(?m)^{name}="([^"]*)"', sh).group(1)

    def ps1_value(name: str) -> str:
        return re.search(rf"(?m)^\${name} = (?:\[version\])?'([^']*)'", ps1).group(1)

    assert sh_value("AIDEVKIT_REF") == ps1_value("AiDevKitPinned")
    assert sh_value("AIDEVKIT_SHA256") == ps1_value("AiDevKitPinnedSha256")
    assert sh_value("MIN_CLI_VERSION") == ps1_value("MinCliVersion")
    assert sh_value("FORGE_GITHUB") == ps1_value("ForgeGitHub")
    assert f'FORGE_GITHUB = "{sh_value("FORGE_GITHUB")}"' in (ROOT / "install_genie_code.py").read_text()
    assert sh_value("CLAUDE_PLUGIN") == ps1_value("ClaudePlugin")

    def ps1_list(name: str) -> list[str]:
        return re.findall(r"'([^']+)'", re.search(rf"(?m)^\${name} = @\((.*)\)", ps1).group(1))

    assert sh_value("KNOWN_TOOLS").split() == ps1_list("KnownTools")
    assert sh_value("LEGACY_SKILLS").split() == ps1_list("LegacySkills")


def test_release_workflow_builds_and_tests():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "tools/build_release.py" in workflow and "pytest" in workflow and "build_forge_catalog.py --check" in workflow
    for asset in ("install.sh", "install.ps1", "install_genie_code.py", "SHA256SUMS", "forge-skills-"):
        assert asset in workflow


def test_powershell_installer_is_ascii():
    # Windows PowerShell 5.1 reads a BOM-less script, and irm decodes a download, in the system code page:
    # a UTF-8 box-drawing character there decodes to a curly quote, which PowerShell treats as a string quote.
    text = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert all(ord(c) < 128 for c in text), sorted({c for c in text if ord(c) > 127})
