# Forge (`forge`)

The entry point for Forge, which builds complete, evaluated Databricks solutions from a plain-language request. Tell it what you want built, and it picks the Forge use case that produces it and hands off to that use case's skill, which does the building. If the working directory already has a build in progress, it offers to pick that up instead. When no use case fits, it points you to the Databricks skill that covers your request, or to `forge-smith` to build a new use case. It builds nothing itself.

## What it's for

- **Choose it for** starting any Forge build: `/forge <what you want>` (`@forge` in Genie Code). A plain request without `/forge`, such as "build a chatbot over the PDFs in my volume", should start here too.
- **Not for** a single platform task, such as one SQL query or one dashboard. It tells you which Databricks skill covers that.

## What you provide

- **A request** that says what you want at the end, such as an agent that answers questions, a search index, values in tables, a measure of guardrails, or model services with their guardrails and access. Anything you already know, such as a volume path, an endpoint, or names, saves a question later.
- **Nothing else.** Type just `/forge` to see the use cases and choose one.
- **A build in progress,** if there is one: a use case's state file in the working directory, such as `doc-index.json`, is how it knows to offer resuming.
- **Before you start:** the Forge skills and the Databricks Agent Skills (v0.2.27 or later), installed side by side. The Forge installer does this.

## What you get

A handoff to one of these use cases, whose own overview says what it needs and what it builds:

| Use case | Skill | What it builds |
|---|---|---|
| Document Q&A agent | [`forge-doc-qa-agent`](../forge-doc-qa-agent/README.md) | A cited Q&A agent over your documents, on Databricks Apps |
| Document search index | [`forge-unstructured-doc-extraction`](../forge-unstructured-doc-extraction/README.md) | An AI Search index over your documents, kept current, with a retrieval evaluation |
| Structured extraction | [`forge-structured-doc-extraction`](../forge-structured-doc-extraction/README.md) | Typed tables of the fields in your documents, with a review queue and an optional review app |
| Guardrail evaluation | [`forge-evaluate-guardrails`](../forge-evaluate-guardrails/README.md) | Recall, precision, and false-positive rates of AI Gateway's built-in guardrails on a safety dataset |
| Model services and access | [`forge-aig-create-model-service`](../forge-aig-create-model-service/README.md) | AI Gateway model services, their guardrails, and access grants, from YAML templates you fill in |

Or, when no use case fits, the name of the skill that covers your request, such as `databricks-vector-search` or `databricks-aibi-dashboards`, or `forge-smith` to turn a codebase into a new use case.

## How it works

| # | Step | What happens |
|---|---|---|
| 1 | Resume | Looks for a build in progress in the working directory, and offers to continue it |
| 2 | Match | Matches your request to the use case by the result you want, not by what you start from |
| 3 | Ask when it's ambiguous | When the request could mean more than one use case, asks one question about the result you want |
| 4 | Two use cases | Runs one at a time, starting with the one the other needs, then offers the other |
| 5 | No matching use case | Points you to the Databricks skill that covers the request |
| 6 | Hand off | Tells you which use case it picked and why, and loads its skill. What you've already said counts toward that skill's questions |

## Before it acts

It starts nothing billable and creates nothing. The use case it hands off to asks for its own go-ahead before anything that costs money. It never builds or suggests Agent Bricks Knowledge Assistant or Information Extraction, which are being deprecated.

## Running it

> /forge build a document Q&A agent over the PDFs in /Volumes/main/legal/contracts
>
> /forge pull the invoice number, vendor, and total from /Volumes/main/ap/invoices into tables

Run `/forge` again in the same folder later to pick up where a build stopped.
