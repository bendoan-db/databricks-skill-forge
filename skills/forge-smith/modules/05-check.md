# 05 · Check

Check the new skill against Forge's rules, add it to Forge when it's in a Forge checkout, and report.

## Inputs → outputs

- **Inputs:** `<out>/<name>/`, and `routed` from `smith.json`.
- **Outputs:** a skill that passes `check_skill.py`; in a Forge checkout, a router entry and passing tests; the report.

## 1. Forge's rules

```bash
uv run <smith>/scripts/check_skill.py <out>/<name> --skills-dir <out>
```

Fix each problem it lists, and run it again until it passes. It also runs the skill's own `check_base_skills.py` against the base skills in `<out>`.

## 2. In a Forge checkout

Only when `routed` is true. The checkout's root is `<out>/..`:

1. **Add the skill to `skills/forge/forge.yaml`,** under `use_cases`: `{name: <name>, min_version: 0.1.0}`.
2. **Add routing cases to `skills/forge/evals/routing.yaml`:** at least two prompts that hand off to the new skill, and one that's close to it but belongs to a neighboring use case.
3. **Regenerate the router's catalog and `MANIFEST.md`:** `uv run tools/build_forge_catalog.py`.
4. **Document it:**
   - Add a row to the README's "Available skills" table.
   - Write `docs/<name>-design.md`: where each source file went (`source.map`), what changed from the source and why, and a "Not yet verified" list.
5. **Run the tests,** and fix what fails:

   ```bash
   uv run --no-project --with pytest --with pyyaml --with pypdf --with rapidfuzz --with scipy pytest -q -p no:cacheprovider tests
   ```

6. **Show the author** `git status` and `git diff --stat`. Don't commit unless they ask.

## 3. Report

Tell the author:
- where the skill is, and its modules, each with the source files it came from;
- the questions its module 00 asks;
- what wasn't ported and why, and any hard-coded secrets found in the source;
- the job's notebooks, and that users can edit them in their workspace (the "Editing the job's notebooks" section);
- that it hasn't run in a workspace, so it needs an end-to-end run, such as its `evals/scenarios.yaml`, before anyone relies on it;
- how to start it: through `forge` in a Forge checkout, or otherwise by its name.

## Verify

- `check_skill.py` passes.
- In a Forge checkout, `uv run tools/build_forge_catalog.py --check` passes, and so do the tests.
- You gave the author the report.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Load lines name …, ucs.yaml declares …` | A Load line was dropped or added while writing the module. Restore the set from the blueprint, or update the module's `base_skills` |
| `smith: marker(s) left` | Module 04, step 4 |
| `check_base_skills.py: … missing` | A base skill or cited reference isn't installed in `<out>` |
| `test_every_use_case_skill_is_routed` fails | The skill isn't in `skills/forge/forge.yaml` (step 2.1) |
| `test_routing_cases` fails | No routing case hands off to the new skill (step 2.2) |
