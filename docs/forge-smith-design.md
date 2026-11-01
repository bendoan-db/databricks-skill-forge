# forge-smith design notes

Maintainer notes for `skills/forge-smith`, the charter's "Use Case Skill Builder: a meta skill.md file that allows Databricks users to combine base skills and reference existing use-case skills to build their own use case". It starts from a codebase that already implements the solution, because that's how the existing skills were built: `forge-structured-doc-extraction` and `forge-unstructured-doc-extraction` came from advance-document-processing (`structured-doc-extraction-design.md` maps its folders to modules). forge-smith turns that process into a workflow an agent can repeat. This file isn't published.

## What kind of skill it is

- **A builder, not a use case:** `forge_kind: builder`, and no `ucs.yaml`. So the use-case tests, the router's catalog, and the drift job's dependency graph all skip it, as they skip the router. Installers and releases ship it like every other folder in `skills/`.
- **No Load lines.** It chooses base skills for the new skill from what's installed: `scan.py --skills-dir` lists each base skill's description and reference titles. Its own text doesn't depend on any base skill file, so upstream releases can't break it, and the drift job has nothing to track.
- **The router points to it,** from the "No matching use case" table (`point_to: forge-smith` in the routing cases), rather than handing off. The router's handoff checks a use case's catalog entry and minimum version, and forge-smith has neither.

## Scripts do the mechanical parts; the agent does the judgment

| Part | Who | Why |
|---|---|---|
| Inventory: files, bundle jobs in task order, settings, secrets, hard-coded names, base-skill signals | `scan.py` | Exhaustive and repeatable; an agent skimming a repo misses settings |
| Stages, module boundaries, which reference file each step needs, the metric | Agent, approved by the author (module 02) | Needs reading the code |
| Discovery questions: classifying each setting | Agent, approved by the author (module 03), with rules | Needs knowing what's user-specific |
| Frontmatter, `ucs.yaml`, Load lines, the Workflow table, module 00, thresholds, `check_base_skills.py` | `scaffold.py` | The values that must agree across files are written once, from one blueprint |
| Steps, ported scripts, the diagram | Agent (module 04), at the `smith:` markers | Content |
| Forge's rules | `check_skill.py` | Usable outside this repo, where the tests aren't |

**Markers.** `scaffold.py` leaves `smith:` markers (an HTML comment in Markdown, a comment in code and YAML) wherever the agent has to write. `check_skill.py` fails while any remain, so a half-filled skill can't pass.

**`check_skill.py` stays in line with the tests.** `test_check_skill_passes_every_use_case_skill` runs it on every hand-built skill. When a repo rule changes, either the hand-built skills or the checker change with it. Where the hand-built skills differ, the checker accepts what they all do. For example, `forge-structured-doc-extraction` names `volume_path` in its commands rather than in a question table, so "asked in module 00" means named there.

**The shared file.** `templates/check_base_skills.py` is one more copy of the shared script. The forge-smith test fails when it drifts from the use-case skills' copies. The drift job's `sync_shared` reads `ucs.yaml` `shared` lists, so it doesn't update this copy. A drift edit to the script fails that test, and the pull request gets marked needs-owner.

## Job code is notebooks

Since 2026-11-01, pipeline and training code in every Forge skill is a Databricks notebook, run as a `notebook_task`, so users can open, run, and edit it in their workspace (owner direction). App code stays Python modules, the platform's practice for apps, and the agent's local tools (`render.py`, `evaluate.py`, `check_base_skills.py`) stay `uv` scripts.
- **Stubs:** `scaffold.py` writes job and training stubs from `templates/notebook.py`, and a `scripts/databricks.yml` stub with a notebook task for each one. A module's `app_scripts` get plain-module stubs.
- **Checks:** `check_skill.py` checks that every job task in `scripts/databricks.yml` is a `notebook_task`, and that the notebook it runs starts with the notebook header and ends with the guarded run cell. `tests/test_skills.py` checks the same on the hand-built skills, and also that each task's `base_parameters` are flags of its notebook's `build_parser()`.
- **The skeleton's helpers** (`notebook_argv`, `script_dir`, `get_spark`) are copies of the shared `parse.py`'s; `tests/test_forge_smith.py` fails when they drift.

## Discovery questions

Module 03 turns every setting into exactly one class: a required question, an optional input, a derived value, a check, a secret by name, or dropped. Its rules come from `forge-unstructured-doc-extraction`'s module 00, the first skill written to ask before building:
- about six questions at most;
- each question says what it's for and its format;
- an answer that names a resource gets a check;
- "create one" is allowed only if the user can create it. Otherwise they must name an existing one.

`scaffold.py` writes the generated module 00 from these classes, with the same seven steps as the hand-built preflights.

## What a generated skill is, and isn't

- **Version 0.1.0, written against the installed base skills.** `ucs.yaml` records the versions found and `source` (the repo, the commit, and where each file went). Its header says it isn't certified.
- **Nothing is run.** The scanned code is read only. The generated scripts aren't executed or tested until the skill runs in a workspace.
- **Joining Forge** happens only in a Forge checkout (`routed: true`). Module 05 then adds the catalog entry and routing cases, regenerates the catalog, writes a design note, and runs the tests. Elsewhere, the skill runs standalone, by name.

## Not yet verified

- **A full run by an agent on a real codebase.** Only the scripts are tested: the scanner on a fixture repo, and the scaffold, then filling the markers, then the checker. The judgment modules (02 to 04) haven't been exercised. A good first test is to rebuild `forge-structured-doc-extraction` from advance-document-processing at `60ab408` and compare.
- **Signal coverage.** `scan.py`'s patterns are heuristics. A codebase that uses a feature through an unusual API may show no signal. Module 01 has the agent confirm signals in the code, and read the stages either way.
- **`databricks workspace export-dir`** of a notebook folder, as module 00's way to bring in workspace code.
