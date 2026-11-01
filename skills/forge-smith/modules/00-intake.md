# 00 · Intake

Find out what to build from, what it should produce, and where the new skill goes. The answers go in `<workdir>/smith.json`.

## 1. Ask the author

Ask in one question, and wait. If the request already answered one, confirm it instead of asking again:

| Ask | Records |
|---|---|
| The codebase: a local folder, a Git URL (with a branch, tag, or commit), or a workspace folder | `codebase` |
| What the solution produces, for whom, in one sentence. For example, "a search index the support app queries", or "fraud scores for each new claim, for the review team" | `end_result` |
| The skill's name and title. Propose `forge-<what it produces>`, in lowercase letters and hyphens | `name`, `title` |
| Where to write it. Default: `<smith>/..`, next to the base skills | `out` |
| Who owns it: the team that fixes it when the base skills change | `owner` |

## 2. Get the code

- **A local folder:** use it in place.
- **A Git URL:** clone it into `<workdir>/source/`, then record that folder as `codebase`, with `source.repo` and `source.commit`. Don't run anything in it: no install, setup, or build scripts.

  ```bash
  git clone --depth 1 [--branch <ref>] <url> <workdir>/source/<repo>
  git -C <workdir>/source/<repo> rev-parse HEAD
  ```

- **A workspace folder:** export it as source files.

  ```bash
  databricks workspace export-dir <workspace-path> <workdir>/source/<name> --profile <profile>
  ```

## 3. Where the skill goes

- **A Forge checkout:** if `<out>/../tools/build_forge_catalog.py` exists, record `routed: true`. Module 05 adds the skill to Forge's catalog.
- **Anywhere else:** record `routed: false`. The user runs the skill by name.
- **The folder must be new:** if `<out>/<name>` exists, ask for another name.
- **The base skills must be next to it:** `<out>/databricks-core/SKILL.md` must exist, because Load lines resolve to sibling folders. If it doesn't, ask the author to install the base skills there, for example with Forge's installer, or `databricks aitools install --path <out>`.
- **Record `upstream`,** the databricks-agent-skills tag the base skills come from: `v` plus the `release` in `databricks aitools list -o json`. Without the CLI, use the newest `upstream` pin in the installed Forge skills' `ucs.yaml` files.

## 4. Write `smith.json`

```json
{
  "codebase": "<folder>",
  "source": {"repo": "<url or null>", "commit": "<sha or null>"},
  "end_result": "<one sentence>",
  "name": "<name>",
  "title": "<title>",
  "owner": "<owner>",
  "out": "<out>",
  "routed": false,
  "upstream": "v0.2.35",
  "completed": []
}
```

## Verify

- `codebase` is a folder you can read.
- `name` matches `^[a-z0-9]+(-[a-z0-9]+)*$`, and `<out>/<name>` doesn't exist.
- `<out>/databricks-core/SKILL.md` exists, and `upstream` is a tag such as `v0.2.35`.
