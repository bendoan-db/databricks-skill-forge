# forge router design notes

Maintainer notes for `skills/forge`, the entry point for Forge use cases. This file sits outside the skill folders, so it isn't published and agents never load it.

## The flow

The user invokes `forge` (`/forge <request>`, or a plain request its description catches), and `forge`:

1. offers to resume a build whose state file is in the working directory;
2. otherwise matches the request to one use-case skill, by the result the user wants;
3. asks one question when the request is ambiguous;
4. points to a base skill when no use case fits;
5. hands off by reading `<forge>/../<skill>/SKILL.md`, and offers the next step when that skill is done.

It's a thin router: it builds nothing, collects no shared inputs, and keeps no state of its own. Shared intake (one `forge.json` for the profile, schema, and volume) was deferred. It would couple every use-case skill to `forge`, and each one still has to run on its own.

## Packaging

`forge` is a separate skill installed next to the use-case skills, not one skill that contains them. When the router was added, the three use-case skills held 58 files and folders, against the 50-entry limit for one skill. Nesting them under `forge/use-cases/<skill>/modules/` would also be four levels deep, against a limit of 3. Separate folders keep each skill publishable, versioned, and certified on its own, and keep the `shared` files and `requires` model.

The handoff reads the sibling folder by path, the same mechanism the **Load:** lines use for base skills. So it doesn't depend on an agent's skill tool, and works wherever the base-skill loads work.

## Names

The use-case skills gained a `forge-` prefix (`forge-doc-qa-agent`, `forge-unstructured-doc-extraction`, `forge-structured-doc-extraction`), and `use-case-skills/` became `skills/`. The prefix groups them in skill listings and in Unity Catalog, and marks them as Forge use cases next to the `databricks-*` base skills. Earlier versions mentioned in these notes (`doc-qa-agent` 0.3.0) carried the unprefixed names. State files, job names, bundle folders, and app names didn't change.

## Keeping the use-case skills from competing with forge

Each use-case skill's description now starts "Forge use case, normally started by the forge skill", and ends "Use when forge hands off to it, or when the user names this skill", so agents don't load it directly for a plain request. The "Prefer it over databricks-…" sentences moved to `forge`'s description, since `forge` is now what competes with the base skills.

The skills still run on their own when loaded directly: nothing in them requires `forge`. Their preflights confirm answers the user already gave instead of asking again, and their Done when returns to `forge` for a next step.

Claude Code has a frontmatter flag that stops a skill from loading automatically, but it wasn't added. Other agents and Unity Catalog skills may not accept the key, and the description change is portable. Revisit this if the routing evals show use-case skills loading first.

## Single source of truth

Each `ucs.yaml` has a `catalog` block: title, when to choose it, what it isn't for, what it produces, its state file, and next steps. `tools/build_forge_catalog.py` renders those blocks, with the minimum versions in `forge.yaml`, into the Use cases and Resume tables in `forge`'s SKILL.md, between generated markers. `tests/test_forge.py` checks that:

- the rendered block is current;
- `forge.yaml` lists every skill folder, and no minimum version is newer than the skill's own;
- every skill a catalog block names exists;
- every routing case names a real skill, question, or fallback base skill.

The generator lives in `tools/`, not in the skill, so publishing `forge` ships only text.

`forge.yaml`'s `use_cases` order is the order the Resume table checks state files in. `forge-doc-qa-agent` comes first because its working directory also holds the index's `doc-index.json`.

## Evals

Routing moved out of each `scenarios.yaml` into `skills/forge/evals/routing.yaml`. Each case expects exactly one of a handoff, a question, or a fallback base skill, plus optional checks for the first skill loaded (requests without `/forge`), deprecation notices, resumes, and offered next steps. The certification scenarios now start through `forge` (`defaults.harness.entry`).

## Not yet verified

- Agents load `forge` first for plain requests, rather than a use-case skill or a base skill. The routing cases without `/forge` measure this.
- `forge`'s description scales to about 6–8 use cases. After that, it should name categories instead of skills.
- Unity Catalog publishing keeps sibling skill folders side by side, as the handoff and the **Load:** lines both assume.
