---
name: forge-structured-doc-extraction
description: "Forge use case, normally started by the forge skill: extracts fields, entities, and line items from the documents in a Unity Catalog volume (PDF, DOCX, PPTX, images) into typed Delta tables, with a citation and confidence score for every value: incremental ai_parse_document parsing, ai_classify v2.1 routing for mixed documents and multi-document packets such as loan or claim files, ai_extract v2.1 per document type, a review queue of low-confidence values, accuracy scoring against labeled documents in MLflow, and optional word-level bounding boxes from OCR. Use when forge hands off to it, or when the user names this skill."
compatibility: Requires databricks CLI (>= v1.11.0), Python 3.11+, uv, and the databricks-agent-skills base skills (>= v0.2.27) installed in the same skills directory as this skill. Word-level localization also needs permission to create a classic job cluster.
metadata:
  version: "0.2.0"
  forge_kind: use-case
---

# Structured document extraction

Turns the documents in a Unity Catalog volume into typed Delta tables: one table per document type, one row per document, one column per field, each value with its confidence score and a citation back to the page it came from. Values the model is unsure of land in a review queue. With labeled documents, the skill measures accuracy before anyone relies on the tables.

This is a use-case skill. It doesn't restate platform details: each module step names the base skill (from databricks-agent-skills) to load for those details, at the point where it's needed.

## Reference architecture

```
/Volumes/<catalog>/<schema>/<volume>/   Lakeflow job <name>-extract (DAB, file-arrival trigger)
  *.pdf *.docx *.png ──► parse.py ────► <prefix>_parsed     ai_parse_document v2, one row per document
                                                            (500-page slices; split pages assembled)
                         extract.py ──► <prefix>_segments   whole documents, or packet pages classified
                                                            with ai_classify v2.1 and grouped
                                    ──► <prefix>_extracted  ai_extract v2.1 per type: citations, confidence
                                    ──► <prefix>_fields     one row per value ──► <prefix>_review (view)
                                    ──► <prefix>_<type>     typed tables (+ <prefix>_<type>__<array>), CDF on
                         localize.py ─► <prefix>_localized  optional: OCR word boxes, classic job cluster
labeled JSON Lines ──► evaluate.py ──► MLflow run: field accuracy, array soft F1, calibration (05)
```

## Conventions

- `<skill>` is the folder that holds this SKILL.md. The base skills are its siblings, for example `<skill>/../databricks-core`.
- This is a Forge use case. The `forge` skill (`<skill>/../forge`) usually starts it and passes along what the user already said. Loaded directly, it runs the same way.
- `<workdir>` is where `extraction.json` and the `<name>-extract/` bundle live. Default: the current directory.
- `<workdir>/extraction.json` holds every resolved value, including the document types and their schemas. Module 00 creates it, each module reads it first, and you record what a module adds before you move on. In commands, `<key>` means the value of `key` in that file, for example `<profile>` or `<prefix>`.
- A **Load:** line names a base skill, and usually one reference file in it, to read at that step. Don't load base skills up front, and read only the file named.
- Scripts declare their own dependencies, so run them with `uv run <skill>/scripts/<file>.py` from any directory. Use `render.py` to fill templates. Never fill `<...>` placeholders in `scripts/` by hand.

## Inputs

Collect these together in one question during module 00. Only `volume_path` is required; propose the rest from a look at the files.

| Input | Default | Notes |
|---|---|---|
| `volume_path` | — (required) | `/Volumes/<catalog>/<schema>/<volume>[/subdir]` |
| `profile` | from `databricks-core` | CLI profile for every command |
| `target_schema` | the volume's `<catalog>.<schema>` | Where the tables are created |
| `name` | derived from the volume name | Lowercase letters, digits, hyphens, ≤40 chars; the bundle is `<name>-extract` |
| `layout` | proposed in module 00 | `single_type`, `one_per_file`, or `packet` (see Decision points) |
| `document_types` | proposed in modules 02–03 | Per type: `description`, `schema`, optional `instructions`, `pages`, `mode` |
| `split_pages` | off | Each folder holds the single-page files of one document |
| `review_threshold` | 0.8 | Values with lower confidence go to the review queue |
| `ground_truth` | none | JSON Lines of labeled documents, for module 05 |
| `localize` | off | Word-level boxes for each value (module 06); runs a classic job cluster |

## Guardrails

- **Confirm before billable or shared actions:** parsing (priced per page), classification and extraction (priced per token, for every document), re-extraction with `full_refresh`, and the OCR job cluster. Show what will run and wait for an explicit go-ahead.
- **Never delete** tables, views, or volumes without separate confirmation that names the exact resource.
- **Don't grant access** to the tables or the review view unless the user names the users or groups.
- **Treat extracted values as sensitive.** Documents like these often hold personal data (names, SSNs, account numbers). In reports, show counts and field names, plus only the values the user asks to see.
- **Never ask for or store tokens.** Use the CLI profile. `extraction.json` holds names and settings only.
- **Don't build or suggest Agent Bricks Information Extraction.** It is being deprecated. Build the pipeline this skill describes.
- **Docs beat base skills when they disagree.** Follow [Base-skill guidance to override](#base-skill-guidance-to-override) and the doc pages the modules cite.

## Workflow

Run the modules in order. Each one ends with a **Verify** step that must pass before you move on. Track progress with this checklist, and add each module's id to `completed` in `extraction.json` when its Verify passes:

- [ ] 00 Preflight: inputs and layout resolved, base skills present, cost approved, `extraction.json` written
- [ ] 01 Ingest and parse: `<prefix>_parsed` has one row per document
- [ ] 02 Classify: labels designed; every segment typed or flagged `other`
- [ ] 03 Extract: schemas designed; `<prefix>_extracted` has a row per typed segment, without errors
- [ ] 04 Tables and review: typed tables populated, review queue reported
- [ ] 05 Evaluate: thresholds met on labeled documents, or confidence profile reported
- [ ] 06 Localize (only when `localize` is on): hit rate ≥ 0.8

| # | Module | Produces | Base skills used |
|---|---|---|---|
| 00 | [Preflight](modules/00-preflight.md) | `extraction.json`, tooling, cost go-ahead | databricks-core, databricks-unity-catalog |
| 01 | [Ingest and parse](modules/01-ingest-parse.md) | `<name>-extract/` bundle, `<prefix>_parsed` | databricks-ai-functions, databricks-unity-catalog, databricks-dabs, databricks-jobs, databricks-serverless |
| 02 | [Classify](modules/02-classify.md) | labels, `<prefix>_segments` (+ `<prefix>_pages` for packets) | databricks-ai-functions |
| 03 | [Extract](modules/03-extract.md) | schemas, `<prefix>_extracted`, `<prefix>_fields` | databricks-ai-functions |
| 04 | [Tables and review](modules/04-tables-review.md) | `<prefix>_<type>` tables, `<prefix>_review` | databricks-unity-catalog |
| 05 | [Evaluate](modules/05-evaluate.md) | MLflow run, pass/fail against thresholds | databricks-mlflow-evaluation |
| 06 | [Localize](modules/06-localize.md) | `<prefix>_localized` word boxes | databricks-jobs, databricks-serverless-migration |

The working directory ends up with the state file, the bundle, and (for module 05) the labels:

```
<workdir>/
├── extraction.json      # resolved values and document types; render.py reads it
├── ground_truth.jsonl   # optional labels for module 05
└── <name>-extract/      # Declarative Automation Bundle: parse → extract (→ localize) job, rendered by render.py
```

**Resuming.** If `<workdir>/extraction.json` exists, read it, confirm its values with the user, and rerun module 00's base-skill check (step 2). Then start at the first module missing from `completed`, or jump in here:

| Situation | Start at |
|---|---|
| Add a document type, or change labels | 02, step 1 |
| Change a type's schema or instructions | 03, step 1 (re-extraction needs `full_refresh`, which re-bills) |
| Evaluate only, with new labels | 05 |
| Add word-level boxes later | 06 |
| New documents in the volume | Nothing to do: the job processes them on arrival |

## Decision points

| Decision | Default | Switch when |
|---|---|---|
| Layout | Proposed from a sample in module 00 | `single_type`: every file is one document of the same type (no classification). `one_per_file`: each file is one document, of several types. `packet`: one file bundles many documents, such as a loan or claim file; pages are classified and grouped |
| Pages per document (packets) | `single`: each page is its own document | A type's documents run over several pages (statements, tax returns, appraisals). Then set `"pages": "multi"` for it, and consecutive pages of that type become one document |
| Split pages | Off | Files are single pages split upstream, one folder per document. Then set `split_pages` to `true` |
| AI function input | `variant`: the parsed document | `ai_extract` or `ai_classify` rejects the segment VARIANT. Then set `extract_input` to `text`. Citations become character spans, so module 06 can't run |
| Schema shape | Scalars and objects | Documents have repeating rows (line items, transactions). Then use an array of objects, which becomes a child table |
| Extraction mode | Default | A type has long documents or many fields, and module 05 shows misses. Then set `"mode": "precision"` for that type |
| Review threshold | 0.8 | Raise it to send more values to people. Lower it only when module 05's calibration shows that values just under it are reliably correct |
| Word-level boxes | Off | Reviewers need to see the exact words behind each value (highlights in a review UI). Then turn on `localize` (module 06) |
| Trigger | File arrival on the volume | Large batches land on a schedule. Then set `schedule_cron` (Quartz syntax) in `extraction.json` and render again |

## Done when

- `<prefix>_parsed` has one row per document, and you reported files that didn't parse.
- Every segment has a document type, or is in `<prefix>_review` as `unclassified`.
- Each type's table is populated, and you reported the review queue counts by reason.
- With labels, module 05 meets its thresholds: classification accuracy ≥ 0.95 (when labels include `doc_type`), field accuracy ≥ 0.9, and array soft F1 ≥ 0.8 (when arrays are labeled). Report the run URL. Without labels, report the confidence profile and say that accuracy is unmeasured.
- When `localize` is on, at least 80% of cited values are localized.
- You told the user the job name, the tables, the review view, and anything you skipped. If `forge` started this skill, return to it for a next step.

## Base-skill guidance to override

Some base-skill guidance is out of date for this use case. In each of these cases, follow this skill:

- **`ai_classify` version:** `databricks-ai-functions` recommends v2.0, with a 128K-token limit and plain-string labels. The [docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_classify) recommend v2.1: it accepts the parsed VARIANT, has a 1M-token context, and returns `response[0].value` with an optional `confidence_score`. Use `map('version', '2.1', ...)`.
- **`ai_extract` citations:** bounding-box citations need VARIANT input from `ai_parse_document`; text input gets character spans instead ([docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_extract)). The base skill's citation example uses text.
- **Page limit:** `databricks-ai-functions` says documents are limited to 500 pages. The [docs](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_parse_document) apply the limit to the pages one call selects with `pageRange`; `parse.py` parses longer PDFs in 500-page slices and stitches them.
- **Agent Bricks Information Extraction:** being deprecated. Don't use it, even where the docs or older examples point to it.
