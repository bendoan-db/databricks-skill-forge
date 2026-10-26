"""The pull request for one Forge skill: its title, labels, and body, built from the run's records."""

import json

LABELS = {"compatible": "drift:compatible", "material": "drift:material", "breaking": "drift:breaking"}


def branch(skill: str) -> str:
    return f"drift/{skill}"


def title(proposal: dict) -> str:
    return f"drift({proposal['forge_skill']}): upstream {proposal['from_tag']} → {proposal['to_tag']}"


def labels(proposal: dict) -> list[str]:
    found = ["drift", LABELS.get(proposal["materiality"], "drift:compatible")]
    if not proposal["valid"]:
        found.append("needs-owner")
    return found


def body(proposal: dict, impacts: list[dict], assessments: list[dict], plan: dict, upstream_repo: str,
         diff_stat: list[str], dry_run_note: str = "") -> str:
    """The pull request description. plan is the model's proposal JSON, or {} for a pin bump."""
    skill, old, new = proposal["forge_skill"], proposal["from_tag"], proposal["to_tag"]
    compare = f"https://github.com/{upstream_repo}/compare/{old}...{new}"
    lines = [dry_run_note] if dry_run_note else []
    action = {"pin_bump": "moves the upstream pin", "edit": "updates the skill"}[proposal["action"]]
    lines += [
        f"Upstream [{upstream_repo}]({compare}) changed between `{old}` and `{new}` in ways that touch `{skill}`. "
        f"This pull request {action} (`{skill}` → {proposal['new_version']}).",
        "",
        f"**Assessed materiality:** {proposal['materiality']}. Opened by `forge_ucs_drift`; review it like any change, "
        "and don't merge it unreviewed.",
        "",
    ]
    if plan.get("summary"):
        lines += ["## Summary", "", plan["summary"], ""]

    lines += ["## What changed upstream", "", "| Base skill | Change | Files | Forge modules |", "|---|---|---|---|"]
    for i in impacts:
        files = ", ".join(f"`{p}`" for p in json.loads(i["paths_json"])) or "—"
        modules = ", ".join(f"`{m}`" for m in json.loads(i["modules_json"]))
        lines.append(f"| `{i['base_skill']}` | {i['kind'].replace('_', ' ')} | {files} | {modules} |")
    lines.append("")

    if assessments:
        lines += ["## Assessment", ""]
        for a in assessments:
            if a.get("error"):
                lines.append(f"- `{a['base_skill']}`: **not assessed** ({a['error'][:200]})")
                continue
            detail = json.loads(a["assessment_json"])
            lines.append(f"- `{a['base_skill']}`: **{a['materiality']}**. {detail.get('summary', '')}")
            for reason in detail.get("reasons", [])[:5]:
                lines.append(f"  - `{reason.get('file', '')}`: {reason.get('change', '')} → {reason.get('effect', '')}")
            if detail.get("resolved_gaps"):
                lines.append(f"  - Upstream now covers: {'; '.join(detail['resolved_gaps'])}")
        lines.append("")

    if plan.get("rationale"):
        lines += ["## Changes and evidence", ""]
        for r in plan["rationale"]:
            lines.append(f"- `{r.get('path', '')}`: {r.get('why', '')}")
            for e in r.get("evidence", [])[:3]:
                lines.append(f"  > {str(e.get('quote', '')).strip()[:400]}  \n  — {e.get('source', '')}")
        lines.append("")

    lines += ["## Files", "", *[f"- {s}" for s in diff_stat], ""]

    failures = json.loads(proposal["failures_json"] or "[]")
    lines += ["## Validation", ""]
    if proposal["valid"]:
        lines.append("- Passed: the repository's tests (compared with `main`), the forge catalog check, and "
                     f"`check_base_skills.py` against upstream `{new}`.")
    else:
        lines.append(f"- **Failed after {proposal['attempts']} attempt(s).** This draft needs its owner:")
        lines += [f"  - {f}" for f in failures[:20]]
    lines.append("- Not run: the end-to-end certification scenarios (`evals/scenarios.yaml`) — the pin is moved, "
                 "but this release isn't certified in a workspace.")
    lines.append("")

    flags = json.loads(proposal["flags_json"] or "[]")
    if flags:
        lines += ["## Review carefully", "", "These added lines in code reach the network, run commands, or touch "
                  "credentials:", "", *[f"- `{f}`" for f in flags[:30]], ""]
    if plan.get("reviewer_notes"):
        lines += ["## Notes for the reviewer", "", plan["reviewer_notes"], ""]
    if plan.get("upstream_proposal"):
        lines += ["## Draft upstream change proposal (not filed)", "", plan["upstream_proposal"], ""]

    lines += ["## Reviewer checklist", "",
              "- [ ] Each change follows from the upstream diff, with evidence",
              "- [ ] Nothing outside this skill changed except what the pipeline keeps in sync (shared files, README, catalog)",
              "- [ ] Code changes, if any, are safe to run",
              "", f"<sub>forge_ucs_drift run `{proposal['run_id']}` · artifacts: `{proposal['artifact_dir']}`</sub>"]
    return "\n".join(lines)
