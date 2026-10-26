"""Checks for install_genie_code.py, the Genie Code notebook installer. A local folder stands in for /Workspace,
and file:// mirrors for GitHub, so nothing needs a workspace or the network."""

import importlib.util
import inspect
import io
import shutil
import tarfile
from pathlib import Path

import pytest
import yaml
from conftest import SKILLS, SKILLS_DIR
from test_installer import cited_references

ROOT = SKILLS_DIR.parent
NOTEBOOK = ROOT / "install_genie_code.py"
USER = "someone@example.com"
MANIFESTS = {s: yaml.safe_load((SKILLS_DIR / s / "ucs.yaml").read_text()) for s in SKILLS}
REQUIRED = sorted({d["name"] for m in MANIFESTS.values() for module in m["modules"]
                   for d in module.get("base_skills", [])})
NEWEST_PIN = max((m["upstream"].rsplit("@", 1)[1] for m in MANIFESTS.values()),
                 key=lambda tag: tuple(int(p) for p in tag[1:].split(".")))


def load(path: Path = NOTEBOOK, name: str = "install_genie_code"):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tar_gz(files: dict[str, bytes], top: str = "databricks-agent-skills-x", links: tuple[str, ...] = ()) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name in links:
            link = tarfile.TarInfo(f"{top}/{name}")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(link)
    return raw.getvalue()


def base_release(tmp_path: Path, version: str = "9.9.9", drop: tuple[str, ...] = ()) -> dict[str, bytes]:
    """An upstream tree: every base skill Forge loads, the files its Load lines cite, an icon, one extra skill,
    and an experimental skill."""
    files = {"README.md": b"upstream\n", "experimental/spark-python-data-source/SKILL.md": b"---\nname: x\n---\n"}
    for name in [*REQUIRED, "databricks-extra"]:
        if name in drop:
            continue
        frontmatter = f'---\nname: {name}\ndescription: x\nmetadata:\n  version: "{version}"\n---\n'
        files[f"skills/{name}/SKILL.md"] = frontmatter.encode()
        files[f"skills/{name}/assets/databricks.png"] = b"\x89PNG\r\n\x1a\n\x00\xff"
    for line in cited_references(tmp_path).read_text().splitlines():
        skill, ref = line.split(" ", 1)
        if skill not in drop:
            files[f"skills/{skill}/{ref}"] = b"reference\n"
    return files


@pytest.fixture
def nb(tmp_path: Path, monkeypatch):
    """The notebook, with /Workspace and the upstream download pointed at tmp_path."""
    module = load()
    mirror = tmp_path / "base-mirror"
    mirror.mkdir()
    (mirror / NEWEST_PIN).write_bytes(tar_gz(base_release(tmp_path)))
    monkeypatch.setattr(module, "BASE_ARCHIVE_URL", mirror.as_uri())
    monkeypatch.setattr(module, "WORKSPACE", tmp_path / "Workspace")
    module.mirror = mirror
    return module


def user_skills(nb) -> Path:
    return nb.WORKSPACE / "Users" / USER / ".assistant" / "skills"


def test_plan_changes_nothing(nb, capsys):
    target = nb.main(user=USER, cwd=ROOT)  # action defaults to plan
    out = capsys.readouterr().out
    assert target == user_skills(nb) and not nb.WORKSPACE.exists()
    assert f"databricks-agent-skills {NEWEST_PIN}" in out and "the checkout in" in out and "Plan only" in out
    assert f"/Users/{USER}/.assistant/skills (your Genie Code user skills" in out


def test_installs_base_and_forge_side_by_side(nb, capsys):
    target = user_skills(nb)
    (target / "databricks-core").mkdir(parents=True)
    (target / "databricks-core" / "stale.md").write_text("from an older install")
    (target / "my-own-skill").mkdir()
    (target / "my-own-skill" / "SKILL.md").write_text("---\nname: my-own-skill\n---\n")
    nb.main("install", user=USER, cwd=ROOT)
    out = capsys.readouterr().out
    for name in ["forge", *SKILLS, *REQUIRED, "databricks-extra"]:
        assert (target / name / "SKILL.md").is_file(), name
    assert (target / "forge" / "SKILL.md").read_bytes() == (SKILLS_DIR / "forge" / "SKILL.md").read_bytes()
    assert not (target / "databricks-core" / "stale.md").exists()  # replaced whole
    assert (target / "my-own-skill" / "SKILL.md").is_file()  # other skills are left alone
    assert not list(target.rglob("*.png")) and not list(target.rglob("__pycache__"))
    assert not (target / "spark-python-data-source").exists()  # experimental skills aren't installed
    assert "Replaces:     databricks-core" in out and "Leaves out:" in out and "files that aren't text" in out
    assert f"export-dir /Users/{USER}/.assistant/skills ~/.genie/skills" in out


def test_skills_folder(nb):
    assert nb.skills_folder("workspace", "", USER) == "/.assistant/skills"
    assert nb.skills_folder("user", "/Workspace/Users/a/forge/", USER) == "/Users/a/forge"
    assert nb.skills_folder("workspace", "/Users/a/forge", USER) == "/Users/a/forge"  # a folder overrides the scope
    assert nb.local("/Users/a/forge") == nb.WORKSPACE / "Users" / "a" / "forge"
    for bad in ("Users/a/forge", "/Workspace", "/"):
        with pytest.raises(nb.InstallError):
            nb.skills_folder("user", bad, USER)
    with pytest.raises(nb.InstallError):
        nb.skills_folder("everyone", "", USER)


def test_custom_folder_is_one_to_register(nb, capsys):
    nb.main("install", skills_dir="/Users/a/forge-skills", user=USER, cwd=ROOT)
    assert (nb.WORKSPACE / "Users" / "a" / "forge-skills" / "forge" / "SKILL.md").is_file()
    assert "First add /Users/a/forge-skills in Genie Code" in capsys.readouterr().out
    with pytest.raises(nb.InstallError, match="is a skill folder"):
        nb.main(skills_dir="/Users/a/forge-skills/forge", user=USER, cwd=ROOT)


def test_warns_when_the_other_folder_has_copies(nb, capsys):
    shared = nb.WORKSPACE / ".assistant" / "skills" / "forge"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text("---\nname: forge\n---\n")
    nb.main(user=USER, cwd=ROOT)
    assert "/.assistant/skills already has 1 of these skills" in capsys.readouterr().out


def test_base_skills_tag(nb):
    with pytest.raises(nb.InstallError, match="databricks-agent-skills v9.9.9"):  # not in the mirror
        nb.main(base_ref="v9.9.9", user=USER, cwd=ROOT)


def test_missing_base_skill_stops_before_writing(nb, tmp_path: Path):
    (nb.mirror / NEWEST_PIN).write_bytes(tar_gz(base_release(tmp_path, drop=("databricks-core",))))
    with pytest.raises(nb.InstallError, match="lacks base skills Forge needs: databricks-core"):
        nb.main("install", user=USER, cwd=ROOT)
    assert not nb.WORKSPACE.exists()


def test_failed_check_stops_before_writing(nb, tmp_path: Path):
    (nb.mirror / NEWEST_PIN).write_bytes(tar_gz(base_release(tmp_path, version="0.0.1")))  # older than written against
    with pytest.raises(nb.InstallError, match="doesn't find its base skills"):
        nb.main("install", user=USER, cwd=ROOT)
    assert not nb.WORKSPACE.exists()


def test_release_notebook_verifies_its_download(nb, tmp_path: Path, capsys):
    spec = importlib.util.spec_from_file_location("build_release", ROOT / "tools" / "build_release.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tag = f"v{(ROOT / 'VERSION').read_text().strip()}"
    dist, mirror = tmp_path / "dist", tmp_path / "release-mirror"
    tool.build(tag, dist)
    (mirror / tag).mkdir(parents=True)
    archive = Path(shutil.copy(dist / f"forge-skills-{tag}.tar.gz", mirror / tag))
    released = load(dist / "install_genie_code.py", "install_genie_code_release")
    released.BASE_ARCHIVE_URL, released.WORKSPACE = nb.BASE_ARCHIVE_URL, nb.WORKSPACE
    released.FORGE_RELEASE_URL = mirror.as_uri()
    elsewhere = tmp_path / "imported"  # a notebook imported on its own, not in a Git folder
    elsewhere.mkdir()
    released.main("install", user=USER, cwd=elsewhere)
    assert f"release {tag}, checksum verified" in capsys.readouterr().out
    assert (user_skills(nb) / "forge" / "SKILL.md").is_file()
    shutil.rmtree(nb.WORKSPACE)
    with archive.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(released.InstallError, match="doesn't match its checksum"):
        released.main("install", user=USER, cwd=elsewhere)
    assert not nb.WORKSPACE.exists()


def test_unpack_refuses_escapes_and_skips_links(nb, tmp_path: Path):
    nb.unpack(tar_gz({"skills/a/SKILL.md": b"ok", "scripts/x.py": b"skip"}, links=("skills/link",)), tmp_path / "out",
              keep="skills")
    assert (tmp_path / "out" / "skills" / "a" / "SKILL.md").read_bytes() == b"ok"
    assert not (tmp_path / "out" / "scripts").exists() and not (tmp_path / "out" / "skills" / "link").exists()
    with pytest.raises(nb.InstallError, match="outside its folder"):
        nb.unpack(tar_gz({"skills/../../evil": b"no"}), tmp_path / "out")
    assert not (tmp_path / "evil").exists()


def test_downloads_are_https_only(nb):
    with pytest.raises(nb.InstallError, match="use an https:// URL"):
        nb.download("http://example.com/x.tar.gz", "x")


def test_notebook_shape():
    text = NOTEBOOK.read_text()
    assert text.startswith("# Databricks notebook source\n") and "\n# COMMAND ----------\n" in text
    run_cell = text.rsplit("# COMMAND ----------", 1)[1]
    assert "dbutils" not in text.rsplit("# COMMAND ----------", 1)[0]  # importing it needs no notebook globals
    assert 'if "dbutils" in globals():' in run_cell
    params = inspect.signature(load().main).parameters
    assert [p for p in params if params[p].kind is not inspect.Parameter.KEYWORD_ONLY] == list(load().WIDGETS)
