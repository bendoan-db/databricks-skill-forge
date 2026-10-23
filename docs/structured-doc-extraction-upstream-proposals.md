# Draft change proposals for databricks-agent-skills (structured-doc-extraction)

Drafts for the base-skill gaps that `use-case-skills/structured-doc-extraction` works around (and, for proposals 1 and 3, `doc-qa-agent` too), in the format of upstream's [change-proposal template](https://github.com/databricks/databricks-agent-skills/blob/main/.github/ISSUE_TEMPLATE/change-proposal.md). Checked against upstream `main` on 2026-10-23 (v0.2.34). Not filed yet. After filing one, add its URL as `upstream_issue` on the matching `coverage_gaps` entry in each `ucs.yaml`.

The Agent Bricks Information Extraction deprecation is left out: no base skill covers it, and there's no public notice to cite yet.

---

## 1. Recommend `ai_classify` version 2.1

**Problem or opportunity.** `databricks-ai-functions/references/1-task-functions.md` recommends `ai_classify` version 2.0, caps input at 128,000 tokens, and shows the response as plain label strings. The docs now recommend version 2.1. It accepts the VARIANT from `ai_parse_document`, has a 1M-token context, and returns per-label objects (`response[0].value`), with `enableConfidenceScores` and `enableRationales` options. Agents that follow the skill read `response[0]::STRING`, which breaks under 2.1.

**Proposed change.** Make 2.1 the recommended version, list its options, show the v2.1 response shape and how to read it, and keep 2.0 and 1.0 as legacy. Update the SQL and PySpark examples, including the document-processing pipeline in `SKILL.md`.

**Affected skill or area.** `databricks-ai-functions` (`SKILL.md`, `references/1-task-functions.md`).

**Additional context.** [ai_classify docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_classify) (updated 2026-10-22).

---

## 2. Say that bounding-box citations need VARIANT input

**Problem or opportunity.** The `ai_extract` version 2.1 section shows citations on a text column. The docs say bounding-box citations (`metadata.citations[].bbox`) need VARIANT input from `ai_parse_document`, and text input returns character spans (`chunk_type: span`). Agents building review UIs or highlighting extract from text and get no boxes.

**Proposed change.** State the rule in the version 2.1 section, show an example on `ai_parse_document` output, and document the `metadata.citations` shape for both kinds. Mention the `mode: precision` option for long documents.

**Affected skill or area.** `databricks-ai-functions` (`references/1-task-functions.md`, `ai_extract` section).

**Additional context.** [ai_extract docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_extract) (updated 2026-10-22).

---

## 3. Document parsing PDFs over 500 pages

**Problem or opportunity.** The `ai_parse_document` section says "max 500 pages per document". The docs apply the limit to the pages selected with `pageRange` ("The selected pages must stay within the 500-page limit"), and a document over 500 pages without `pageRange` "fails immediately". Agents conclude that longer documents can't be parsed.

**Proposed change.** Explain that longer documents can be parsed in slices of up to 500 pages with `pageRange`, that the option takes a literal (one statement per slice), and that the outputs must be stitched, with page ids renumbered, for downstream functions that expect one document.

**Affected skill or area.** `databricks-ai-functions` (`references/1-task-functions.md`, `ai_parse_document` limits).

**Additional context.** [ai_parse_document docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_parse_document) (updated 2026-10-22). Before filing, confirm in a workspace that a slice past page 500 parses, and how its page ids are numbered.
