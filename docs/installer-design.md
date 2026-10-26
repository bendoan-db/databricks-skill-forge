# Installer design notes

Maintainer notes for `install.sh` (macOS and Linux) and `install.ps1` (Windows), for the Genie Code notebook `install_genie_code.py`, and for how releases are built. The shell installers install the base skills and the Databricks MCP server through [AI Dev Kit](https://github.com/databricks-solutions/ai-dev-kit), and then the Forge skills. Nothing here is published with any skill.

## Where the Forge skills come from

The installer picks a source in this order:

| Source | When | Verified |
|---|---|---|
| A branch or tag's GitHub source archive | `--forge-ref REF` (development) | No, and the plan says so |
| The release's `forge-skills-<tag>.tar.gz` | The installer came from a release: `FORGE_VERSION` is stamped | Against `FORGE_SKILLS_SHA256`, stamped in the same build |
| The checkout's `skills/` | The script sits next to `skills/forge/SKILL.md` | Not needed |
| `main`'s GitHub source archive | An unstamped script outside a checkout, such as a raw `main` URL | No; it warns and prints the release one-liner |

A downloaded release installer always installs its own release, even when it's run from inside a checkout. Nothing is installed until the skills archive has passed its check: the archive is downloaded and verified before the plan is printed.

## Releases

`tools/build_release.py <tag>` writes `dist/`:
- **`forge-skills-<tag>.tar.gz`:** `skills/` only, under a `forge-skills/` folder.
- **`install.sh`, `install.ps1`, and `install_genie_code.py`:** the tag and the archive's SHA-256 written into the `FORGE_VERSION`/`FORGE_SKILLS_SHA256` lines (`$ForgeVersion`/`$ForgeSkillsSha256` in PowerShell). Each pattern must match exactly once, so an already-stamped file or a renamed line fails the build.
- **`SHA256SUMS`** and **`RELEASE_NOTES.md`**, which carries the install one-liners pinned to the tag, and each skill's version.

**The archive is reproducible.** Entries are sorted, timestamps are fixed at 1980-01-01, owners are 0, permissions are normalized to 644 or 755 (only the execute bit is kept), the gzip header has no timestamp, and caches are left out. So a commit always produces the same checksum, and `tests/test_release_build.py` builds twice and compares.

**Versions are semver.** `VERSION` holds the version, and the tag must be `v<VERSION>`; `check_tag` enforces both. This version belongs to the bundle, and each skill still has its own version in its `ucs.yaml`.

**`.github/workflows/release.yml`:**
- On every pull request and push to `main`, it runs the tests:
  - on Linux: everything, plus the catalog check and shellcheck;
  - on macOS: everything, under the real bash 3.2;
  - on Windows: the PowerShell installer's tests, under PowerShell 7 and Windows PowerShell 5.1.
- On a `v*` tag, it then builds the release on Linux and publishes it with `gh release create`.

`.gitattributes` forces LF line endings everywhere, so a Windows checkout has the same bytes.

**Repository.** It stays under `bendoan-db` for now, so the one-liner URLs (`releases/latest/download/install.sh`) include that account. Moving to an org later changes them: GitHub redirects a transferred repo, but the README and old release notes would still show the old URLs.

## AI Dev Kit

**Download, not clone.** The installer downloads GitHub's source archive of the pinned tag (`v0.2.0`) and checks it against a pinned SHA-256, the same in both installers (tested). The checksum was taken from two downloads of the archive on 2026-10-25, and the short URL form (`tar.gz/v0.2.0`) returns the same bytes as `tar.gz/refs/tags/v0.2.0`. GitHub doesn't promise those archive bytes never change; if it changes them, the install stops with a checksum error. In that case, re-pin `AIDEVKIT_SHA256` in both installers after checking the new archive matches the tag's contents. `--ai-dev-kit-ref` installs any other ref unverified, and the plan says so.

**One persistent copy.** The MCP server runs from a virtual environment inside the AI Dev Kit copy, and each agent's MCP config points at that copy's Python. So it lives at a stable path, `~/.databricks-skill-forge/ai-dev-kit`, and a `.forge-source` file records which ref and checksum it is. An upgrade:

1. unpacks the new copy to `ai-dev-kit.new`;
2. moves the current copy to `ai-dev-kit.old`, and moves the new one into place;
3. reruns the MCP installer, which rebuilds the virtual environment;
4. deletes `.old` only if that succeeds. If the skills or MCP step fails, `.old` is moved back, so the existing MCP server keeps working.

The copy left by the earlier git-based installer has no `.forge-source`, so it's replaced the same way. With `--no-mcp`, AI Dev Kit is unpacked into a temporary folder and the persistent copy isn't touched.

**What runs.** The checkout's `install.sh` (or `install.ps1`) installs the base skills:

```
--tools <list> -p <profile> --skills <every stable skill, plus MLflow> --experimental false --force --silent [--global]
```

with `DEVKIT_SCOPE=<scope>`, from the project directory. Then `databricks-mcp-server/mcp_install.sh` (or `mcp_install.ps1`, whose profile parameter is `-Profile_`) registers the MCP server.

## Decisions

- **Named skills, not AI Dev Kit's "all" profile.** For Claude Code, Codex, and Copilot, "all" runs the native `databricks aitools install`, which registers a plugin (`databricks@claude-plugins-official`) instead of writing skill folders. Forge's **Load:** lines, handoffs, and `check_base_skills.py` all expect the base skills as sibling folders. Named skills (`--skills`) go through `--skills-only`, which writes folders. The list is every stable skill from `databricks aitools list -o json` (the same live inventory AI Dev Kit reads), plus the MLflow list from AI Dev Kit's installer. That's what "all" would install, as folders. If the plugin is already installed, the installer warns and prints the uninstall command, but never removes it.
- **The MCP server has its own installer.** AI Dev Kit 0.2.0 moved MCP setup out of `install.sh` into `databricks-mcp-server/mcp_install.sh`. Upstream calls the server deprecated and maintains it on a best-effort basis. Forge runs it by default because the MCP setup was part of the request. `--no-mcp` skips it, and a failed MCP install is only a warning, because no Forge skill depends on the server.
- **One confirmation.** The installer prints its plan and asks once. AI Dev Kit runs with `--silent`, which is the only way to skip its own prompts: it reads `/dev/tty` even when stdin is redirected. `--force` makes reruns reapply the named set; without it, an unchanged AI Dev Kit version exits early with "up to date". With no terminal to confirm on, the installer stops unless `--yes` (`-Yes`) is passed.
- **Downloads.**
  - Only `https://` URLs are accepted. `file://` is also accepted, for mirrors and tests.
  - `curl` runs with `--proto =https --tlsv1.2`, and PowerShell enables TLS 1.2.
  - The `FORGE_*_URL` and `FORGE_AIDEVKIT_SHA256` settings exist for mirrors. Anyone who can set them can also change `PATH`, so they don't weaken the checks.
- **Skills directories copy AI Dev Kit's.** `tool_dir` (and `Get-ToolDir`) mirror AI Dev Kit's per-agent directories. Forge only installs where `databricks-core` actually landed, so a path that drifts in a future AI Dev Kit shows up as a "No base skills in <dir>" warning, not a broken install.
- **macOS bash 3.2.** No associative arrays, `mapfile`, or case-changing expansions. Folder lists go through files and `while read`, so paths with spaces work.

## The Windows installer

`install.ps1` mirrors `install.sh` step for step, with these differences:

- **No `param` block.** It parses `$args` itself, and accepts both `-Tools` and `--tools`. That way `irm … | iex` (no arguments) and `& ([scriptblock]::Create((irm …))) -Tools …` (arguments) both work, as AI Dev Kit's installer does.
- **Nothing is left in the caller's session.** The whole script runs in one script block, and `exit` is called only when it was run as a file: inside `iex`, `exit` would close the user's window. `DEVKIT_SCOPE` is restored afterwards.
- **Windows PowerShell 5.1 quirks:**
  - It turns redirected stderr from native commands into errors, so those calls run with errors not set to stop.
  - It reads a BOM-less script in the system code page, so the file is ASCII only (tested).
- **`tar`.** It uses `%SystemRoot%\System32\tar.exe` when there is one, because Git for Windows' `tar`, if it comes first on `PATH`, reads `C:\` paths as remote hosts.
- **Child installers.** AI Dev Kit's installers run in the same PowerShell edition as the installer itself, with `-ExecutionPolicy Bypass`.

## The Genie Code notebook

Genie Code in the workspace loads skills from `/Users/<user>/.assistant/skills`, `/.assistant/skills`, and folders added in its settings ([docs](https://docs.databricks.com/aws/en/genie-code/skills)). The Genie Code CLI (Beta) loads them only from local folders: `~/.genie/skills`, and a project's `.agents/skills` ([docs](https://docs.databricks.com/aws/en/genie-code/genie-code-cli)). Neither has an installer for skill folders, and the shell installers can't write to the workspace, so `install_genie_code.py`, a notebook in source format, does it.

- **Forge sources:** the same order as the shell installers: a Forge ref; the stamped release, checked against its checksum; the checkout, when the notebook runs from a Git folder (its working directory has `skills/forge/SKILL.md`); otherwise `main`, unverified.
- **Base skills come from the upstream archive, not AI Dev Kit.** A notebook has no `databricks` CLI or terminal. So the notebook downloads databricks-agent-skills' GitHub source archive and takes `skills/` (the stable skills; `experimental/` is left out).
  - **The tag** is the newest `upstream` pin among the Forge skills, not the latest release. The skills were written against it, and the drift job moves the pins, so the notebook follows them without a pin of its own.
  - **The archive isn't checksum-verified,** because its tag is chosen at run time. That's the same trust the shell installers give `databricks aitools`.
- **Text only.** Files that aren't UTF-8 text are left out (in v0.2.34, one PNG icon per base skill, for plugin listings), so every folder meets the text-only limit `tests/test_skills.py` holds Forge's own skills to. Genie Code's docs don't state limits.
- **Checked before anything is written.** Everything is staged in a temporary folder, and each `check_base_skills.py` runs against the stage. The workspace folder is written only after that passes, and the check runs again on the installed copy.
- **Plan by default.** The Action widget defaults to `plan`, the notebook's version of the shell installers' one confirmation. A folder with the same name as an incoming skill is replaced whole, and the plan names each one. Other skills in the folder are left alone. The plan warns when the other Genie Code folder (user or workspace) already has some of these skills, since Genie Code loads both.
- **Writes go through `/Workspace`,** one file at a time, without copying permissions or times. That's why it needs serverless compute or Databricks Runtime 14.3 LTS or later: those support writing workspace files, and run a notebook in its own folder, which is how it finds a checkout.
- **Downloads** are `https://` only (`file://` for mirrors and tests), and so are redirects. `FORGE_RELEASE_URL`, `FORGE_ARCHIVE_URL`, and `FORGE_BASE_ARCHIVE_URL` point it at a mirror.
- **No MLflow skills and no MCP server.** No Forge skill loads either, and Genie Code adds MCP servers in its settings only.
- **The CLI.** The notebook prints a `databricks workspace export-dir` command that copies the install to `~/.genie/skills`. The CLI also ships Databricks skills in `~/.genie/skills/.system/`. Forge needs its base skills as siblings, so the copies stay.
- **Tests** (`tests/test_installer_notebook.py`) import the notebook, with a local folder standing in for `/Workspace` and `file://` mirrors for GitHub. All `dbutils` and `spark` calls are in the last cell, which runs only where `dbutils` is defined.

## Not yet verified

- A real install end to end, on any OS. The tests use stand-ins for `databricks`, `uv`, and AI Dev Kit, and the bash installer has only been dry-run against a real CLI (v1.20.0, agent skills 0.2.30).
- `install.ps1` under PowerShell at all. It was written without PowerShell available, and checked only by review, a rough brace count, and the stamping and consistency tests. Its tests run for the first time in GitHub Actions.
- A real release. No `v*` tag has been pushed, so `releases/latest/download/…` returns 404 until the first one.
- That `databricks aitools update` keeps the named folders current after an install.
- `mcp_install.sh` and `mcp_install.ps1` with `--silent` and no terminal.
- The Genie Code notebook in a workspace:
  - writing `.assistant` folders through `/Workspace` on serverless;
  - egress to GitHub from serverless;
  - importing the release's `install_genie_code.py` as a notebook.
- How Genie Code treats two skills with the same name: a base skill in both the user and workspace folders, or next to the CLI's `.system` copy.
- `databricks workspace export-dir` with these files and `--overwrite`. It doesn't remove files that a newer version dropped.
- Whether the use-case workflows run in Genie Code in a notebook, since their modules run scripts with `uv run` and deploy with the `databricks` CLI.
