# Databricks notebook source
# MAGIC %md
# MAGIC # Install Forge for Genie Code
# MAGIC
# MAGIC Puts the Databricks base skills ([databricks-agent-skills](https://github.com/databricks/databricks-agent-skills)) and the Forge skills side by side in a Genie Code skills folder, then checks that each use-case skill finds the base skills it loads.
# MAGIC
# MAGIC 1. **Run all.** With **Action** set to `plan` (the default), the notebook downloads and checks everything and shows what it would change. It changes nothing.
# MAGIC 2. **Set Action to `install`**, and run all again.
# MAGIC 3. **Start a new Genie Code chat**, then type `@forge` and say what you want built.
# MAGIC
# MAGIC | Widget | Default | Use |
# MAGIC |---|---|---|
# MAGIC | Install for | `user` | `user`: `/Users/<you>/.assistant/skills`, for you only. `workspace`: `/.assistant/skills`, for everyone in the workspace, which takes admin rights on that folder |
# MAGIC | Skills folder | | Install into this workspace folder instead, then add it in Genie Code's settings under skills (**Add folder**) |
# MAGIC | Base skills tag | the newest tag the Forge skills are pinned to | Install another databricks-agent-skills tag |
# MAGIC | Forge ref | | Install a Forge branch or tag instead, unverified (for development) |
# MAGIC
# MAGIC Run it on serverless compute, or on Databricks Runtime 14.3 LTS or later, with outbound access to `github.com` and `codeload.github.com`. The **Genie Code CLI** reads skills only from your computer, so the last step prints the command that copies this install there.

# COMMAND ----------

import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

try:
    import yaml
except ImportError:
    raise ImportError("PyYAML isn't installed here. Run `%pip install pyyaml` in a new cell, "
                      "then run all again.") from None

FORGE_VERSION = ""        # release tag; tools/build_release.py stamps it (empty in a checkout)
FORGE_SKILLS_SHA256 = ""  # SHA-256 of that release's forge-skills archive; stamped with it
FORGE_GITHUB = "bendoan-db/databricks-skill-forge"
BASE_GITHUB = "databricks/databricks-agent-skills"
FORGE_RELEASE_URL = os.environ.get("FORGE_RELEASE_URL", f"https://github.com/{FORGE_GITHUB}/releases/download")
FORGE_ARCHIVE_URL = os.environ.get("FORGE_ARCHIVE_URL", f"https://codeload.github.com/{FORGE_GITHUB}/tar.gz")
BASE_ARCHIVE_URL = os.environ.get("FORGE_BASE_ARCHIVE_URL", f"https://codeload.github.com/{BASE_GITHUB}/tar.gz")
WORKSPACE = Path("/Workspace")                  # where workspace folders are mounted on the notebook's compute
USER_SKILLS = "/Users/{user}/.assistant/skills"  # Genie Code's user skills: only you see them
WORKSPACE_SKILLS = "/.assistant/skills"          # Genie Code's workspace skills: everyone sees them
SKIP = {"__pycache__", ".ruff_cache", ".pytest_cache", ".DS_Store"}
WIDGETS = {  # name: (default, choices, label); a text widget when there are no choices
    "action": ("plan", ["plan", "install"], "Action"),
    "scope": ("user", ["user", "workspace"], "Install for"),
    "skills_dir": ("", None, "Skills folder (optional)"),
    "base_ref": ("", None, "Base skills tag (optional)"),
    "forge_ref": ("", None, "Forge ref, unverified (optional)"),
}


class InstallError(Exception):
    pass


def step(title: str) -> None:
    print(f"\n{title}")


def msg(text: str) -> None:
    print(f"  {text}")


def ok(text: str) -> None:
    print(f"  ✓ {text}")


def warn(text: str) -> None:
    print(f"  ! {text}")


class HttpsOnly(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another https:// URL, as the shell installers' curl --proto =https does."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise InstallError(f"Refusing a redirect to {newurl}: use an https:// URL")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, what: str) -> bytes:
    """https only (file:// for mirrors and tests)."""
    if not url.startswith(("https://", "file://")):
        raise InstallError(f"Refusing to download {what} from {url}: use an https:// URL")
    try:
        with urllib.request.build_opener(HttpsOnly).open(url, timeout=120) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as e:
        raise InstallError(f"Couldn't download {what} from {url} ({e}). The notebook's compute needs outbound "
                           "access to github.com and codeload.github.com.") from e


def unpack(data: bytes, dest: Path, keep: str = "") -> None:
    """Unpack a .tar.gz's single top-level folder into dest: regular files only, and only the ones under keep."""
    keep_parts = PurePosixPath(keep).parts if keep else ()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if member.name.startswith("/") or ".." in parts:
                raise InstallError(f"Refusing an archive entry outside its folder: {member.name}")
            parts = parts[1:]
            if not member.isfile() or parts[:len(keep_parts)] != keep_parts:
                continue  # links and folders are skipped; folders are made for the files
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())


def skill_names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if (p / "SKILL.md").is_file()) if folder.is_dir() else []


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def skills_folder(scope: str, skills_dir: str, user: str) -> str:
    """The workspace path to install into, as Genie Code shows it (/Users/...). Accepts /Workspace/... too."""
    if not skills_dir:
        if scope not in ("user", "workspace"):
            raise InstallError(f"Install for must be user or workspace, not {scope!r}")
        return USER_SKILLS.format(user=user) if scope == "user" else WORKSPACE_SKILLS
    path = skills_dir.strip().rstrip("/")
    if path == "/Workspace" or path.startswith("/Workspace/"):
        path = path[len("/Workspace"):]
    if not path.startswith("/") or not path.strip("/"):
        raise InstallError(f"The skills folder must be a workspace folder such as /Users/{user}/skills, "
                           f"not {skills_dir!r}")
    return path


def local(path: str) -> Path:
    return WORKSPACE / path.lstrip("/")


def forge_source(work: Path, cwd: Path, forge_ref: str) -> tuple[Path, str]:
    """The Forge skills folder, and where it came from: a ref, this release, this checkout, or main."""
    if forge_ref:
        unpack(download(f"{FORGE_ARCHIVE_URL}/{forge_ref}", f"Forge {forge_ref}"), work / "forge", keep="skills")
        return work / "forge" / "skills", f"the {forge_ref} source, unverified (Forge ref)"
    if FORGE_VERSION:
        data = download(f"{FORGE_RELEASE_URL}/{FORGE_VERSION}/forge-skills-{FORGE_VERSION}.tar.gz",
                        f"the Forge {FORGE_VERSION} skills")
        actual = hashlib.sha256(data).hexdigest()
        if actual != FORGE_SKILLS_SHA256:
            raise InstallError(f"The Forge {FORGE_VERSION} skills archive doesn't match its checksum "
                               f"(expected {FORGE_SKILLS_SHA256}, got {actual}). Nothing was installed.")
        unpack(data, work / "forge")
        return work / "forge", f"release {FORGE_VERSION}, checksum verified"
    if (cwd / "skills" / "forge" / "SKILL.md").is_file():  # a Git folder: the notebook runs from the checkout
        return cwd / "skills", f"the checkout in {cwd}"
    warn("This notebook isn't from a release, so it installs the main branch, unverified. For a release, import")
    warn(f"https://github.com/{FORGE_GITHUB}/releases/latest/download/install_genie_code.py instead.")
    unpack(download(f"{FORGE_ARCHIVE_URL}/main", "Forge main"), work / "forge", keep="skills")
    return work / "forge" / "skills", "the main source, unverified"


def stage(src: Path, dest: Path) -> list[str]:
    """Copy a skill folder, minus caches and anything that isn't UTF-8 text; return what was left out."""
    left_out = []
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if not path.is_file() or path.suffix == ".pyc" or SKIP & set(rel.parts):
            continue
        data = path.read_bytes()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            left_out.append(f"{src.name}/{rel.as_posix()}")
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(data)
    return left_out


def check(skills_dir: Path, use_cases: list[str]) -> list[str]:
    """Run each use-case skill's check_base_skills.py against skills_dir; return the failures."""
    failures = []
    for name in use_cases:
        script = skills_dir / name / "scripts" / "check_base_skills.py"
        result = subprocess.run([sys.executable, "-B", str(script), "--skills-dir", str(skills_dir)],
                                check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if result.returncode:
            failures.append(f"{name}: {(result.stderr.strip().splitlines() or ['the check failed'])[-1]}")
    return failures


def copy_skill(src: Path, dest: Path) -> None:
    """Replace dest with src. Files are written one by one, without copying permissions or times, which
    workspace folders don't keep."""
    if dest.exists():
        shutil.rmtree(dest)
    for path in sorted(src.rglob("*")):
        if path.is_file():
            target = dest / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())


def main(action: str = "plan", scope: str = "user", skills_dir: str = "", base_ref: str = "", forge_ref: str = "",
         *, user: str, cwd: Path) -> Path:
    """Install, or with action "plan" only show what would change. Returns the local path of the skills folder."""
    if action not in ("plan", "install"):
        raise InstallError(f"Action must be plan or install, not {action!r}")
    target_path = skills_folder(scope, skills_dir, user)
    target = local(target_path)
    if (target / "SKILL.md").is_file():
        raise InstallError(f"{target_path} is a skill folder. Give the folder that holds skill folders instead.")

    with tempfile.TemporaryDirectory(prefix="forge-") as tmp:
        work = Path(tmp)
        step("Preparing (nothing is installed until every check passes)")
        forge_dir, forge_from = forge_source(work, cwd, forge_ref)
        forge_names = skill_names(forge_dir)
        use_cases = [name for name in forge_names if (forge_dir / name / "ucs.yaml").is_file()]
        if not use_cases:
            raise InstallError(f"No Forge skills found in {forge_dir}")
        manifests = {name: yaml.safe_load((forge_dir / name / "ucs.yaml").read_text()) for name in use_cases}
        ok(f"Forge: {' '.join(forge_names)} from {forge_from}")

        # The base skills at the newest tag a Forge skill is pinned to: a tag the skills were written against,
        # unlike the latest release, which may have moved a file a module reads.
        tag = base_ref or max((m["upstream"].rsplit("@", 1)[1] for m in manifests.values()), key=version_key)
        unpack(download(f"{BASE_ARCHIVE_URL}/{tag}", f"databricks-agent-skills {tag}"), work / "base", keep="skills")
        base_dir = work / "base" / "skills"
        base_names = skill_names(base_dir)  # skills/ holds the stable skills; experimental/ isn't installed
        required = {dep["name"] for m in manifests.values() for module in m["modules"]
                    for dep in module.get("base_skills", [])}
        if missing := sorted(required - set(base_names)):
            raise InstallError(f"databricks-agent-skills {tag} lacks base skills Forge needs: {', '.join(missing)}. "
                               "Leave Base skills tag empty, or pick a newer tag.")
        ok(f"Base skills: {len(base_names)} stable skills from databricks-agent-skills {tag}, downloaded unverified")

        staged, left_out = work / "stage", []
        for folder, names in ((base_dir, base_names), (forge_dir, forge_names)):
            for name in names:
                left_out += stage(folder / name, staged / name)
        if failures := check(staged, use_cases):
            raise InstallError("A use-case skill doesn't find its base skills, so nothing was installed: "
                               + "; ".join(failures))
        ok("Each use-case skill finds every base skill and reference file it loads")

        names = base_names + forge_names
        step("Plan")
        audience = {USER_SKILLS.format(user=user): "your Genie Code user skills, for you only",
                    WORKSPACE_SKILLS: "the Genie Code workspace skills, for everyone in the workspace"}
        msg(f"Install into: {target_path} ({audience.get(target_path, 'a folder you add in Genie Code settings')})")
        msg(f"Skills:       {len(base_names)} base skills ({tag}) and {len(forge_names)} Forge skills")
        if replaced := [name for name in names if (target / name).exists()]:
            msg(f"Replaces:     {' '.join(replaced)} (folders of the same name, replaced whole)")
        if left_out:
            msg(f"Leaves out:   {len(left_out)} files that aren't text, such as {left_out[0]}; "
                "skill folders stay text only")
        for other in {USER_SKILLS.format(user=user), WORKSPACE_SKILLS} - {target_path}:
            try:
                twice = [name for name in names if (local(other) / name / "SKILL.md").is_file()]
            except OSError:  # a folder this user can't read
                twice = []
            if twice:
                warn(f"{other} already has {len(twice)} of these skills, such as {twice[0]}. Genie Code loads both "
                     "folders, so it will see two copies.")
        if action == "plan":
            print("\nPlan only: nothing was changed. Set Action to install, then run all again.")
            return target

        step("Installing")
        try:
            target.mkdir(parents=True, exist_ok=True)
            for name in names:
                copy_skill(staged / name, target / name)
        except PermissionError as e:
            hint = (" The workspace skills folder takes workspace admin rights or CAN MANAGE on it; install for user "
                    "instead." if target_path == WORKSPACE_SKILLS else "")
            raise InstallError(f"Can't write to {target_path} ({e}).{hint}") from e
        ok(f"{len(names)} skills → {target_path}")
        if failures := check(target, use_cases):
            raise InstallError("The installed copy failed its check: " + "; ".join(failures))
        ok("Each installed use-case skill finds its base skills")

    step("Done")
    msg("Start a new Genie Code chat (an open chat keeps the skills it started with), then type @forge and say")
    msg("what you want built, for example: @forge build a document Q&A agent over the PDFs in /Volumes/main/docs/pdfs")
    if target_path not in audience:
        msg(f"First add {target_path} in Genie Code: Settings (the gear icon), skills, Add folder.")
    msg("Genie Code CLI: it reads skills only from your computer. To copy this install there:")
    msg(f"  databricks workspace export-dir {target_path} ~/.genie/skills --overwrite --profile <profile>")
    return target


# COMMAND ----------

if "dbutils" in globals():  # a notebook run; the tests import this file and call main() themselves
    for name, (default, choices, label) in WIDGETS.items():
        if choices:
            dbutils.widgets.dropdown(name, default, choices, label)  # noqa: F821 (a notebook global)
        else:
            dbutils.widgets.text(name, default, label)  # noqa: F821
    main(**{name: dbutils.widgets.get(name).strip() for name in WIDGETS},  # noqa: F821
         user=spark.sql("SELECT current_user()").first()[0], cwd=Path.cwd())  # noqa: F821
