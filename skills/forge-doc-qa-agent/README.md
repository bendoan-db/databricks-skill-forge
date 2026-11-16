# Document Q&A agent (`forge-doc-qa-agent`)

Builds a document Q&A (RAG) agent that answers questions about the user's documents and cites the file and page behind each answer, as `[file_name, p. N]`. The agent is a LangGraph agent, built with the Agent Bricks CLI and deployed on Databricks Apps (Agent Runtime), with managed sessions for multi-turn conversations. It searches an AI Search index of the documents with hybrid search, choosing metadata filters itself. If the documents aren't indexed yet, the skill builds the index first with `forge-unstructured-doc-extraction`. Before anyone relies on the agent, it's evaluated with MLflow judges.

## Purpose

- **This Forge Skill is used to:**
  - build an agent or chatbot that answers questions about documents and cites its sources: "chat with these files", an assistant for a policy library, or a RAG app;
  - build the documents' search index first, when they aren't indexed yet.
- **Not for** only a search index: that's `forge-unstructured-doc-extraction`. **Not for** field values in tables: that's `forge-structured-doc-extraction`.

## How to Run

The user starts it with `/forge` (`@forge` in Genie Code) and says what they want, or names the skill. The coding assistant works through it step by step, and asks before anything that costs money.

> /forge Build a document Q&A agent over the PDFs in /Volumes/main/legal/contracts and deploy it as a Databricks App.

Its progress is saved in `doc-qa.json`, next to the index's `doc-index.json`. The user can run `/forge` in the same folder later to pick up where it stopped, or to change the prompt, the model, or the retrieval settings. New documents need no redeploy: the agent searches the index live.

## Inputs

**Prerequisites:**
- **The documents:** an index of them built by `forge-unstructured-doc-extraction`, or documents in a Unity Catalog volume, which it indexes first. Indexing them asks for that skill's inputs: the volume path, an AI Search endpoint, the index name, and whether the index stays current.
- **Tools:**
  - the Databricks CLI (v1.11.0 or later), signed in to the workspace;
  - `uv`;
  - `databricks-agentbricks` (v0.5.x);
  - the Databricks Agent Skills (v0.2.27 or later) and `forge-unstructured-doc-extraction`, installed next to this skill.

  The Forge installer sets up all but `databricks-agentbricks`.
- **Privileges:**
  - `SELECT` on the index;
  - permission to create Databricks Apps;
  - `EXECUTE` on the chat model's `system.ai` model service.

  The skill checks these first.

**What it asks the user.** None of these are required: each has a default the skill proposes, and the user confirms.

| Input | Required | Default | What it is |
|---|---|---|---|
| `index_state` | No | `doc-index.json` in the working directory | The index's settings, written by `forge-unstructured-doc-extraction`. Missing? The skill builds the index first |
| `name` | No | the index's name | Names the agent project and its app, `agent-bricks-<name>`. Lowercase letters, digits, and hyphens, at most 17 characters (app names are limited to 30) |
| `chat_model` | No | Claude Sonnet 5.5 (`system.ai.claude-sonnet-5-5`) | The model the agent answers with, through Unity AI Gateway |
| `eval_set` | No | `evals.jsonl` from the index | Test questions, optionally with expected answers or facts |

## Outputs

**In the workspace:**
- **The app:** `agent-bricks-<name>`, a Databricks App running the agent, with a chat UI and an API.
  - Its service principal has `SELECT` on the index.
  - Everyone the app is shared with gets answers from the whole index.
- **Sessions:** managed sessions that keep each conversation's state.
- **Traces and evaluations:** an MLflow experiment holding the agent's traces and its evaluation runs.

**In the working directory:**

```
<workdir>/
├── README.md        # what was built, its settings, and each step's status
├── doc-index.json   # the index's settings (from forge-unstructured-doc-extraction)
├── evals.jsonl      # the test questions
├── <name>-index/    # the index's job bundle
├── doc-qa.json      # the agent's settings
└── <name>/          # the Agent Bricks agent project (agent.toml, agent/, app.yaml)
```

**When it's done.** The deployed app returns a cited answer, and the MLflow judges score:
- groundedness ≥ 0.8: answers are supported by the passages retrieved;
- relevance ≥ 0.8: answers address the question;
- correctness ≥ 0.6, when the test questions have expected answers or facts.

## How It Works

| # | Step | What happens |
|---|---|---|
| 00 | Preflight | Finds the index, or builds it with `forge-unstructured-doc-extraction`, then resolves the inputs, checks the tools and the user's privileges, and gets the user's go-ahead on cost |
| 01 | Agent | Scaffolds the agent with the Agent Bricks CLI, gives it the document search tool and a grounding prompt, and tests it locally until its answers cite their sources |
| 02 | Deploy as a Databricks App | Deploys the agent to Agent Runtime, grants its service principal access to the index, and checks that a live answer matches the local one |
| 03 | Evaluate | Scores the agent's answers with MLflow judges, and tunes it if they fall short |

Each step ends with a check that must pass before the next one starts.

### Preflight Checks and Guardrails

It asks before:
- building the index, which asks for its own go-ahead;
- deploying the app;
- each evaluation run.

It shares the app only with the users or groups the user names. It never deletes an app, index, table, or endpoint without a separate confirmation that names it.
