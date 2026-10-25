# install.sh design notes

Maintainer notes for the repo-root `install.sh`, which installs the base skills and the Databricks MCP server through [AI Dev Kit](https://github.com/databricks-solutions/ai-dev-kit), and then the Forge skills. This file isn't published with any skill.

## What it runs

1. **Base skills:** AI Dev Kit's `install.sh`, from a checkout at a pinned release (`v0.2.0`), run in the project directory (or with `--global`):

   ```
   DEVKIT_SCOPE=<scope> install.sh --tools <list> -p <profile> --skills <every stable skill, plus MLflow> --experimental false --force --silent [--global]
   ```

   AI Dev Kit hands the base skills to `databricks aitools install --skills-only`, and links them into each agent's skills directory.
2. **MCP server:** `databricks-mcp-server/mcp_install.sh` from the same checkout, with the same tools, profile, and scope. It builds a virtual environment inside the checkout and registers the `databricks` server in each agent's MCP config. So the checkout has to persist: it lives in `~/.databricks-skill-forge/ai-dev-kit`, not in a temporary folder.
3. **Forge skills:** every folder in `skills/` is copied into each skills directory that has `databricks-core`, replacing an existing copy. Folders under the names used before the `forge-` prefix are removed only if their SKILL.md has `forge_kind`, so a user's own skill under one of those names stays. Then each use-case skill's `check_base_skills.py` runs against that directory.

## Decisions

- **Named skills, not AI Dev Kit's "all" profile.** For Claude Code, Codex, and Copilot, "all" runs the native `databricks aitools install`, which registers a plugin (`databricks@claude-plugins-official`) instead of writing skill folders. Forge's **Load:** lines, handoffs, and `check_base_skills.py` all expect the base skills as sibling folders. Named skills (`--skills`) go through `--skills-only`, which writes folders. The list is every stable skill from `databricks aitools list -o json` (the live inventory AI Dev Kit reads, too), plus `MLFLOW_SKILLS` from the checkout's `install.sh`. That's what "all" would install, as folders. If an existing plugin is found, the installer warns and prints the uninstall command, but never removes it.
- **The MCP server needs its own installer.** AI Dev Kit 0.2.0 dropped MCP setup from `install.sh` (`--mcp` only prints a notice) and moved it to `databricks-mcp-server/mcp_install.sh`. Upstream calls the server deprecated and maintains it on a best-effort basis. The installer runs it by default, because the MCP setup was part of the request. `--no-mcp` skips it, and a failed MCP install is a warning, not an error, because no Forge skill depends on the server.
- **One confirmation.** The installer prints its plan and asks once. AI Dev Kit runs with `--silent`, which is the only way to skip its own prompts: it reads `/dev/tty` even when stdin is redirected. Silent mode also hides AI Dev Kit's progress, so the installer reports results itself. `--force` makes reruns reapply the named set; without it, an unchanged AI Dev Kit version exits early with "up to date". With no terminal to confirm on, it stops unless `--yes` is passed.
- **Pinned AI Dev Kit release.** The flags and behavior above were read from v0.2.0 (the release) and `main` (commit `026662a`, 2026-10-23). The only differences between them are in Gemini CLI and Kiro handling. Before raising the pin, re-check `--skills`, `--experimental`, `--silent`, `--force`, `DEVKIT_SCOPE`, the per-tool skills directories (`agent_skill_target_dirs`), `MLFLOW_SKILLS`, and `mcp_install.sh`'s flags.
- **Skills directories copy AI Dev Kit's.** `tool_dir` mirrors `agent_skill_target_dirs` in AI Dev Kit's `install.sh`. Forge only installs where `databricks-core` actually landed, so a path that drifts in a future AI Dev Kit shows up as a "No base skills in <dir>" warning, not a broken install.
- **macOS bash 3.2.** No associative arrays, `mapfile`, or case-changing expansions. Folder lists go through files and `while read`, so paths with spaces work. `tests/test_installer.py` runs the script under `/bin/bash`.

## Not yet verified

- A real install end to end. The tests use stand-ins for `databricks`, `uv`, and AI Dev Kit, and only a dry run has run against a real CLI (v1.20.0, agent skills 0.2.30).
- That `databricks aitools update` keeps the named folders current after the install.
- The `mcp_install.sh` run with `--silent` and no terminal.
- Windows. There's no PowerShell installer yet; AI Dev Kit has `install.ps1` and `mcp_install.ps1` to build one from.
