# Model services and access (`forge-aig-create-model-service`)

Sets up Unity AI Gateway model services, their guardrails, and who can use them, from YAML files you fill in. Before writing anything, it checks that you hold the Unity Catalog privileges the changes need. Then it writes three templates, with every setting explained in their comments, and waits while you fill them in, or while you describe what you want and it fills them in for you. It checks the filled-in files against the workspace, shows you every change it would make, and only after your go-ahead creates or updates the services, attaches their guardrails in order, and grants access. Running it again makes the workspace match the files.

It's a Forge use case. Start it with `/forge` (`@forge` in Genie Code) and say what you want, or name the skill. Your coding assistant works through it step by step, and asks before it changes anything.

## What it's for

- **Choose it for:**
  - creating or changing model services: their routing, traffic splits between models, fallbacks, rate limits, and inference tables;
  - putting guardrails (service policies) on them: built-in jailbreak, unsafe-content, hallucination, and sensitive-data policies, and your own LLM-judge or SQL rules;
  - granting account groups, users, and service principals access to model services, new or existing.
- **Not for** measuring how well guardrails block unsafe prompts: use `forge-evaluate-guardrails`. **Not for** spend budgets, MCP services, or model provider services: use the `databricks-unity-gateway` base skill.

## What you provide

**Before you start:**
- **Workspace:** a workspace in a region with Unity AI Gateway, and an existing Unity Catalog schema for the services.
- **Guardrails:** a workspace admin must turn on the **Unity Gateway Service Policies** preview. SQL guardrails also need a SQL warehouse.
- **Tools:**
  - the Databricks CLI (v1.11.0 or later), signed in to the workspace;
  - `uv`;
  - the Databricks Agent Skills (v0.2.27 or later), installed next to the Forge skills.
- **Privileges:** the skill checks them for you, first on the schema and again on everything your files name, and tells you what's missing:
  - `USE CATALOG`, `USE SCHEMA`, and `CREATE SERVICE`, to create services;
  - `CREATE TABLE`, for inference tables;
  - `CREATE FUNCTION`, for SQL guardrails;
  - `EXECUTE` on each model a service routes to, and on each guardrail's evaluator;
  - `MANAGE` or ownership, to update an existing service or grant access to it.

**What it asks you.** First, where the services go and what to set up. Everything else about the services, guardrails, and grants goes in the YAML files.

| Input | Required | Default | What it is |
|---|---|---|---|
| `target_schema` | Yes | — | `<catalog>.<schema>` where new services, their inference tables, and SQL guardrail functions are created |
| `parts` | Yes | — | What to set up, any of: model services (`model_services`), guardrails on them (`service_policies`), and access (`grants`) |
| `profile` | No | your CLI profile | The CLI profile every command uses, and whose privileges are checked |
| `config_dir` | No | `ai-gateway` | The folder, in your working directory, for the YAML files |
| `default_evaluator` | No | `system.ai.gpt-oss-20b` | The model service that runs LLM-based guardrails, unless one names its own |
| `warehouse_id` | No | your default SQL warehouse | The warehouse that creates SQL guardrail functions |
| `test_prompt` | No | "What is Databricks? Answer in one sentence." | The prompt sent to each service to check that it answers |

**The YAML files you fill in:**
- **`model-services.yaml`:** each service's name and description, the models it routes to, and its settings.
  - Settings: a traffic split, fallbacks, a per-user rate limit, an inference table, and its guardrails, in order.
  - It starts with one service on Claude Sonnet 5.5's model, `system.ai.databricks-claude-sonnet-5-5`, falling back to `system.ai.gpt-oss-120b`.
- **`service-policies.yaml`:** the guardrails, defined once and listed by name on each service. Jailbreak detection, unsafe-content detection, and PII redaction are defined already, with a plain-language rule and a SQL rule as examples.
- **`permissions.yaml`:** each group, user, or service principal, the services it can use, and its privileges (default `EXECUTE`).

## What you get

**In your workspace:**
- **The model services:** each one as `<catalog>.<schema>.<name>`, with the routing, fallbacks, rate limits, and guardrails your file describes, and an inference table, `<name>_payload`, if you asked for one.
- **SQL guardrail functions:** one Unity Catalog function per SQL guardrail.
- **Access:** the grants in `permissions.yaml`, including `USE CATALOG` and `USE SCHEMA` on each service's catalog and schema. Only what's missing is granted.

**In your working directory:**

```
<workdir>/
├── README.md                # what was built, its settings, and each step's status
├── aig-model-services.json  # the settings
└── ai-gateway/              # the YAML files you fill in
```

**When it's done.** Every service matches its file, every service answers a test prompt through the gateway, and every grant is in effect: all three rates ≥ 1.0. A reply blocked by a guardrail counts as an answer, since the guardrail did its job. A guardrail that can't reach a verdict doesn't: it denies every request it checks.

## How it works

| # | Step | What happens |
|---|---|---|
| 00 | Preflight | Asks where the services go and what to set up, checks the schema, the gateway, the tools, and your privileges, and gets your go-ahead |
| 01 | Template | Writes the YAML files, and waits while you fill them in |
| 02 | Check and plan | Checks the files against the workspace, checks your access to everything they name, and shows you every change it would make, read-only |
| 03 | Apply | Creates or updates the services with only the fields that differ, attaches their guardrails, and grants what's missing |
| 04 | Evaluate | Checks that each service matches its file and answers, and that each grant is in effect |

Each step ends with a check that must pass before the next one starts.

## Before it acts

It changes nothing until you've seen the plan and said yes. A model service isn't billed by itself, but every request through it is billed by the model it routes to, and every request an LLM-based guardrail checks is billed by its evaluator. It never grants you privileges you're missing. It never revokes access or deletes a service, unless you ask and then confirm the exact principal, privilege, or service. Taking someone out of `permissions.yaml` doesn't revoke their access.

## Running it

> /forge Set up two AI Gateway model services for our POC team, with jailbreak and PII guardrails, and give the data-science group access.

Its progress is saved in `aig-model-services.json`. To change something later, edit the YAML files, or ask your assistant to, and run `/forge` in the same folder: it plans the difference, and applies it after your go-ahead.
