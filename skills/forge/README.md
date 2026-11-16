# Forge (`forge`)

The entry point for Forge, which builds complete, evaluated Databricks solutions from a plain-language request. The user tells it what they want built, and it picks the Forge use case that produces it and hands off to that use case's skill, which does the building. If the working directory already has a build in progress, it offers to pick that up instead. When no use case fits, it points the user to the Databricks skill that covers the request, or to `forge-smith` to build a new use case. It builds nothing itself.

## Purpose

- **This Forge Skill is used to:**
  - start any Forge build, from a plain-language request;
  - pick up a build already in progress in the working directory;
  - find the Databricks skill that covers a request no use case does.
- **Not for** a single platform task, such as one SQL query or one dashboard. It names the Databricks skill that covers that.

## How to Run

The user starts it with `/forge <what they want>` (`@forge` in Genie Code). A plain request without `/forge`, such as "build a chatbot over the PDFs in my volume", should start here too.

> /forge build a document Q&A agent over the PDFs in /Volumes/main/legal/contracts
>
> /forge pull the invoice number, vendor, and total from /Volumes/main/ap/invoices into tables

Running `/forge` again in the same folder later picks up where a build stopped.

## Inputs

- **A request** that says what the user wants at the end, such as an agent that answers questions, a search index, values in tables, a measure of guardrails, or model services with their guardrails and access. Anything the user already knows, such as a volume path, an endpoint, or names, saves a question later.
- **Nothing else.** Just `/forge` lists the use cases to choose from.
- **A build in progress,** if there is one: a use case's state file in the working directory, such as `doc-index.json`, is how it knows to offer resuming.
- **Prerequisites:** the Forge skills and the Databricks Agent Skills (v0.2.27 or later), installed side by side. The Forge installer does this.

## Outputs

A handoff to one of these use cases, whose own overview says what it needs and what it builds:

| Use case | Skill | What it builds |
|---|---|---|
| Document Q&A agent | [`forge-doc-qa-agent`](../forge-doc-qa-agent/README.md) | A cited Q&A agent over the user's documents, on Databricks Apps |
| Document search index | [`forge-unstructured-doc-extraction`](../forge-unstructured-doc-extraction/README.md) | An AI Search index over the user's documents, kept current, with a retrieval evaluation |
| Structured extraction | [`forge-structured-doc-extraction`](../forge-structured-doc-extraction/README.md) | Typed tables of the fields in the user's documents, with a review queue and an optional review app |
| Guardrail evaluation | [`forge-aig-evaluate-guardrails`](../forge-aig-evaluate-guardrails/README.md) | Recall, precision, and false-positive rates of AI Gateway guardrails, built-in or the user's own, on a safety dataset, with several setups compared side by side |
| Model services and access | [`forge-aig-create-model-service`](../forge-aig-create-model-service/README.md) | AI Gateway model services, their guardrails, and access grants, from YAML templates the user fills in |

Or, when no use case fits, the name of the skill that covers the request, such as `databricks-vector-search` or `databricks-aibi-dashboards`, or `forge-smith` to turn a codebase into a new use case.

## How It Works

| # | Step | What happens |
|---|---|---|
| 1 | Resume | Looks for a build in progress in the working directory, and offers to continue it |
| 2 | Match | Matches the request to the use case by the result the user wants, not by what they start from |
| 3 | Ask when it's ambiguous | When the request could mean more than one use case, asks one question about the result the user wants |
| 4 | Two use cases | Runs one at a time, starting with the one the other needs, then offers the other |
| 5 | No matching use case | Points the user to the Databricks skill that covers the request |
| 6 | Hand off | Tells the user which use case it picked and why, and loads its skill. What the user has already said counts toward that skill's questions |

### Preflight Checks and Guardrails

It starts nothing billable and creates nothing. The use case it hands off to asks for its own go-ahead before anything that costs money. It never builds or suggests Agent Bricks Knowledge Assistant or Information Extraction, which are being deprecated.
