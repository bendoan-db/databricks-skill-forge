# Project README design notes

Owner direction, 2026-11-10: running a Forge use case also writes a README into the working directory that fully explains the project and its modules. Every use-case skill carries the shared `scripts/write_readme.py`, and every skill forge-smith builds gets it too. This file isn't published.

## A script writes it, not the agent

`write_readme.py` builds `<workdir>/README.md` from the skill's own text and the state file, the way `render.py` builds the bundle. The same state always gives the same README. It can't drift from the skill or the state, it costs no model tokens, and the tests check it offline. An agent-written README would paraphrase the skill differently on every run, and nothing could check it.

| README section | Comes from |
|---|---|
| Title, summary | ucs.yaml `catalog.title` and `catalog.produces`, and the state's `name` |
| Status | ucs.yaml `modules`, and the state's `completed` |
| Files in this folder | the `<workdir>/` tree in the SKILL.md Workflow section, with names filled in |
| Overview, Architecture | the SKILL.md text before Reference architecture, and its diagram |
| Settings | each state key and value, with the SKILL.md Inputs table's notes |
| Modules | the Workflow table (Produces) and checklist (Done when), and from each module: its `# NN · Title`, purpose, Inputs → outputs, step headings (or the bold leads of a `## Steps` list), and its other sections in full, such as "How the parse stage works" and "Editing the job's notebooks" |
| Decisions, Done when, Changing it later | the SKILL.md Decision points table, Done when bullets, and resume table |
| Next steps | `catalog.next` use cases that aren't in the folder yet |

**Left out on purpose:** Guardrails, Base-skill guidance to override, Verify, Troubleshooting, and Done when bullets that start "You told the user". They tell the agent what to do and aren't useful to someone reading the project later. Each module section names its module file for the full steps.

**Placeholders.** `<key>` is filled from the state file in prose, tables, and the file tree. Dotted keys work (`<review.app_name>`). A use case that requires another also fills keys from the other's state file, the way `forge-doc-qa-agent` reads `<index_name>` from `doc-index.json`. Keys without a value stay as written. The architecture diagram keeps its placeholders, so its columns stay aligned, and the line under it says what each one is in this project. `<workdir>` becomes `./` before a path and "this folder" otherwise ("its own folder", "the same folder").

## When it runs

Each time a module's Verify passes, module 00's included. The rule is a paragraph in each SKILL.md Workflow section, next to the rule that records `completed`, rather than a step in each of the 24 modules. So the README exists as soon as the state file does, and it reflects reruns and resumed builds. Done when checks that it ran after the last module.

## One README per folder

A folder can hold more than one use case: `forge-doc-qa-agent` runs `forge-unstructured-doc-extraction` in the same `<workdir>`. The README covers every use case whose state file is in the folder. It finds them by reading the sibling skill folders next to the script, which the installers always put together. Required use cases come first. Whichever skill runs it, the result is the same, so the index's module runs don't drop the agent's section.

- **Shared tree entries** are described by the first skill that lists them, which is the required one. `forge-doc-qa-agent`'s tree lists the index's bundle as `<name>-index/`, but in its own state file `name` is the agent's name.
- **An index in another folder:** when a state value names a required skill's state file (`index_state: ../index/doc-index.json`), that use case is covered too, and its files are left out of the folder's tree.

## Hand edits, and READMEs that aren't Forge's

- **Notes are the user's.** The text between the `forge:notes` markers is kept on every update.
- **An edit outside Notes wins.** The first line records a checksum of the rest, with Notes blanked. If it doesn't match, the README is kept and listed under `kept_edited`, as `render.py` does with edited job notebooks, and the agent tells the user. `--force` rewrites it and keeps Notes.
- **Never someone else's README.** The working directory is often the root of the user's own repo. A `README.md` without Forge's first line is left alone, and the project's goes to `FORGE-README.md`. If both exist and neither is Forge's, the script stops and asks for one to be moved aside.

## Shared and checked

- `scripts/write_readme.py` is listed under `shared` in every `ucs.yaml`, and must be byte-identical across skills and in `skills/forge-smith/templates/`. As with `check_base_skills.py`, the drift job's `sync_shared` updates the skills' copies but not the template, so a drift edit to the script fails the template test and needs the owner.
- `tests/test_skills.py` checks that every skill carries it, that the Workflow section runs it with the skill's state file, that the tree lists `README.md`, and that Done when names it. `check_skill.py` applies the same rules, so skills forge-smith builds follow them. `scaffold.py` writes the Workflow paragraph, the tree (with a marker for the files the modules create), and the Done when bullet.
- `tests/test_write_readme.py` runs every skill's copy, and checks that each skill's text has what the README reads: module headings, purposes, steps, a checklist line and a Workflow row per module, and a tree.

## What doesn't write one

- **The `forge` router** builds nothing and keeps no state. The use case it hands off to writes the README.
- **forge-smith's own runs** (`smith.json`) don't write one either. What they produce is a skill folder, documented by its SKILL.md and, in a Forge checkout, by `docs/<name>-design.md`. A README in the skill folder would be published with the skill.

## Not yet verified

- Writing `README.md` into a `/Workspace` folder from Genie Code, and how the workspace file browser renders it, including the HTML comments.
- Whether agents run the script after every module in practice, and not only at the end. The certification scenarios should check the README after a resumed build.
