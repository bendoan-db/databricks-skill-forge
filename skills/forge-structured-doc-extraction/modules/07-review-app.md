# 07 · Review app

Only when the user said yes to the review app in module 00 (`review_app` is `true`). With no, skip this module: nothing for the app is created. This module deploys an app where people approve or correct the values in the review queue, and syncs each decision back to the typed tables. It runs after the pipeline modules, in the same build.

**Docs:** [Lakebase synced tables](https://docs.databricks.com/aws/en/oltp/projects/sync-tables) (triggered syncs), [Lakehouse Sync](https://docs.databricks.com/aws/en/oltp/projects/lakehouse-sync) (now titled Lakebase Change Data Feed), and [table update triggers](https://docs.databricks.com/aws/en/jobs/trigger-table-update).

## Inputs → outputs

- **Inputs:** the typed tables and `<prefix>_fields` (module 03), `review_threshold`, and the users or groups who will review.
- **Outputs:**
  - `review` in `extraction.json`;
  - the review queue: `<prefix>_review_candidates` and its synced table, `<prefix>_review_queue`;
  - the app `<review.app_name>`, deployed from `<workdir>/<review.app_name>/`;
  - Lakehouse Sync of the app's decisions;
  - the `review_sync` job and `<prefix>_decisions`.

## How it works

```
<prefix>_fields ─► <prefix>_review_candidates ══ synced table (TRIGGERED, job task review_queue) ══► Postgres <schema>.<prefix>_review_queue
 (confidence < review_threshold)                                                               │ app reads
reviewer ─► app ─► Postgres <prefix>_review.<prefix>_decisions ══ Lakehouse Sync ══► lb_<prefix>_decisions_history
                                                                     table update trigger ─► review_sync job
                    <prefix>_decisions ◄── extract.py --stage decisions ──► typed tables (in place)
```

- **The queue is the review view's low-confidence values.** Each run of the extraction job merges them into `<prefix>_review_candidates`. Its `review_queue` task then refreshes the synced copy the app reads.
- **Decisions are made against one parse of a document.** A re-parsed (changed) file goes back in the queue. A re-extraction of the same parse keeps its decisions.
- **The app writes only its own table:** one row per value, with `row_version` for concurrent reviewers and a request id so a retry isn't saved twice. Lakehouse Sync records every change in an SCD Type 2 history table, which is the audit trail.
- **Applying a decision:**
  - The `review_sync` job copies the latest decision per value into `<prefix>_decisions` and updates the document's typed rows: the value (an approved value stays the same), `<column>__confidence` = 1.0, and the field in `reviewed_fields`.
  - Every extraction applies decisions too, so they survive `full_refresh`.
  - `<prefix>_extracted` and `<prefix>_fields` keep the model's output, so module 05 still measures the model.

## Steps

1. **Confirm the inputs, then the cost.** Don't ask again whether the user wants the app: module 00 settled that.
   - Confirm `review.reviewers` and `review.app_name` from module 00. Only the reviewers get access, in step 7. If the app was added after module 00 (resuming with `review_app` turned on), ask for them now. `review.app_name` is at most 26 characters of `[a-z0-9-]`, because AppKit adds `dev-` in development.
   - `schema` and `prefix` must be lowercase letters, digits, and `_`, because the queue's Postgres names come from them; `render.py` checks.
   - Then list what this module creates, the list the user approved in module 00, and wait for a yes:
     - a Lakebase project (step 2), billed while its compute runs; it scales to zero when idle;
     - a synced table, whose pipeline runs once per extraction job run;
     - Lakehouse Sync;
     - the app, billed while it runs;
     - the `review_sync` job, one short serverless run per batch of decisions.

2. **Lakebase project.** **Load:** `databricks-lakebase` (projects, branches, and databases).
   - Ask whether to reuse a project or create one. Creating one runs `databricks postgres create-project <app_name> --json '{"spec": {"display_name": "<name> review"}}' --profile <profile>`.
   - Use its `production` branch and its `databricks_postgres` database. The synced table, the app, and Lakehouse Sync all use the same database.
   - Record `review.project`, plus `review.branch` and `review.database`, the full resource names from `list-branches` and `list-databases`.

3. **The review queue.** Render, deploy, and run the extraction task, which creates `<prefix>_review_candidates` and `<prefix>_decisions`. It calls AI functions only for documents parsed since the last extraction, as any job run would:

   ```bash
   uv run <skill>/scripts/render.py --state <workdir>/extraction.json   # its summary names every table below
   cd <workdir>/<name>-extract
   databricks bundle deploy --profile <profile>
   databricks bundle run doc_extraction --only extract --profile <profile>
   ```

   Create the synced table, in `TRIGGERED` mode, from the candidates table. **Load:** `databricks-lakebase` `references/synced-tables.md`.

   ```bash
   databricks postgres create-synced-table <catalog>.<schema>.<prefix>_review_queue --json '{"spec": {
     "source_table_full_name": "<catalog>.<schema>.<prefix>_review_candidates", "primary_key_columns": ["review_id"],
     "scheduling_policy": "TRIGGERED", "branch": "<review.branch>", "postgres_database": "databricks_postgres",
     "create_database_objects_if_missing": true}}' --profile <profile>
   databricks postgres get-synced-table synced_tables/<catalog>.<schema>.<prefix>_review_queue --profile <profile> -o json \
     | jq -r '.. | .pipeline_id? // empty' | head -1
   ```

   Record that id as `review.sync_pipeline_id`. Then render and deploy again, which adds the `review_queue` task, and run it once: `databricks bundle run doc_extraction --only review_queue --profile <profile>`.

4. **The app.** Scaffold an AppKit app with the Lakebase and Files features. **Load:** `databricks-apps` `references/appkit/lakebase.md` (scaffolding, schema ownership, reading synced tables).

   ```bash
   databricks apps init --name <app_name> --features lakebase,files --output-dir <workdir> --run none \
     --set "lakebase.postgres.branch=<review.branch>" --set "lakebase.postgres.database=<review.database>" \
     --set "files.files.path=<volume>" --profile <profile>   # <volume>: catalog.schema.volume, from volume_path
   uv run <skill>/scripts/render.py --state <workdir>/extraction.json
   cd <workdir>/<app_name>
   rm -r server/routes/lakebase client/src/pages   # the template's samples; nothing imports them now
   pnpm install
   databricks apps validate --profile <profile>
   ```

   - `render.py` copies `server.ts`, `review-routes.ts`, and `App.tsx` from `<skill>/scripts/review-app/` into the app. It also sets `REVIEW_QUEUE_TABLE` and `REVIEW_DECISIONS_TABLE` in `app.yaml`.
   - The copies track the skill. `render.py` refuses to overwrite one that was edited by hand after it was copied, unless you pass `--force`.
   - Use the package manager the scaffold chose (`packageManager` in `package.json`). Don't change the AppKit version it set.

   After the user confirms, deploy: `databricks apps deploy --profile <profile>`. **Load:** `databricks-apps` `references/platform-guide.md` (deploy, service principal permissions, user access). On its first start, the app creates `<prefix>_review.<prefix>_decisions` and owns it.

5. **Let the app read the queue.** The sync owns the queue's Postgres schema, so the app's service principal can't read it until the project owner grants access. Get the principal from `databricks apps get <app_name> --profile <profile>` (`service_principal_client_id`), then confirm and run:

   ```bash
   databricks psql --project <review.project> --profile <profile> -- -d databricks_postgres -c \
     'GRANT USAGE ON SCHEMA "<schema>" TO "<sp_client_id>"; GRANT SELECT ON "<schema>"."<prefix>_review_queue" TO "<sp_client_id>";'
   ```

6. **Sync decisions back.** Confirm, then turn on Lakehouse Sync for the app's schema. **Load:** `databricks-lakebase` `references/lakehouse-sync.md` (CDF configs, the database resource path, prerequisites).

   ```bash
   databricks postgres create-cdf-config <review.database> <catalog> <schema> <prefix>_review \
     --cdf-config-id <app_name>-decisions --profile <profile>
   ```

   - Lakehouse Sync skips empty tables, so `<catalog>.<schema>.lb_<prefix>_decisions_history` appears only after the first decision. Approve one value in the app with the user (step 8), then wait until `list-cdf-statuses` shows the table streaming and the history table exists.
   - Then record `review.history_ready: true`, and render and deploy again. That adds the `review_sync` job, triggered by updates to the history table. Run it once for the decisions made before it existed: `databricks bundle run review_sync --profile <profile>`.
   - If the catalog has default storage, Lakehouse Sync can't write there. Set `review.history_schema` to a `<catalog>.<schema>` that can, and create the config there instead.

7. **Share the app** with the reviewers from step 1 only. Use `databricks apps update-permissions <app_name> --json '{"access_control_list": [{"group_name": "<group>", "permission_level": "CAN_USE"}]}' --profile <profile>` (`user_name` for a person). Anyone who can open the app sees every queued value, because synced tables don't carry Unity Catalog row filters or column masks.

8. **Try one decision of each kind** with the user: approve one value, and correct another with a note. Within a few minutes, both reach the typed table (see Verify).

## Verify

```sql
SELECT decision, count(*) AS decisions, max(merged_at) AS last_merged FROM <catalog>.<schema>.<prefix>_decisions GROUP BY decision;

SELECT path, reviewed_fields, reviewed_at FROM <catalog>.<schema>.<prefix>_<type> WHERE reviewed_fields IS NOT NULL LIMIT 10;
```

- The app opens for a reviewer and lists documents with open values. A value's page opens in the document pane.
- After step 8, `<prefix>_decisions` has one `approved` and one `corrected` row. The corrected value is in its typed column with `__confidence` = 1.0, and its field is in `reviewed_fields`.
- Decided values are gone from the `<prefix>_review` view, and `<prefix>_extracted` is unchanged.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `render.py`: `review_app needs schema … ` | The synced table puts the Unity Catalog schema name in Postgres, which allows `[a-z0-9_]` only. Use a target schema with such a name (modules 00 and 01) |
| `create-synced-table`: change data feed is required | `<prefix>_review_candidates` doesn't exist yet: run step 3's extract task first |
| App says `permission denied for schema <schema>` | Step 5 hasn't run, or ran before the synced table finished its first sync. Run it again |
| App says `permission denied for schema <prefix>_review` | The schema was created by another identity, such as a local run before the first deploy. **Load:** `databricks-lakebase`, Schema Permissions for Deployed Apps. Dropping the schema deletes decisions, so confirm, and export first |
| Queue in the app is behind the tables | It refreshes when the extraction job's `review_queue` task runs. Run `databricks bundle run doc_extraction --only review_queue` |
| Decisions don't reach the tables | Check, in order: the CDF config is online; `lb_<prefix>_decisions_history` has the rows; the `review_sync` job ran; its task's output. Before `review.history_ready`, the job doesn't exist |
| `lb_<prefix>_decisions_history` doesn't exist | Lakehouse Sync skips empty tables: make one decision in the app first. Or `REPLICA IDENTITY FULL` is missing because the app's table setup failed; check the app's logs |
| `create-cdf-config`: catalog has default storage | Set `review.history_schema` (step 6) |
| Document pane is blank or downloads the file | Browsers show PDFs and images inline. Other types (DOCX, PPTX) download instead |
| Document pane returns 403 | The files plugin reads as the reviewer when `databricks.yml` lists `files.files` under `user_api_scopes`. The reviewer needs `READ VOLUME` on the volume. Grant it only to the reviewers the user named |
| `apps validate` fails on a type error in `server.ts` | The AppKit version's API differs from the one the review app was written against. Run `npx @databricks/appkit docs` for the plugin's current shape, and report it to the skill owner |
| `apply_decisions` failed with a concurrent-modification error | An extraction run rewrote the same document at the same time. The task retries; the next run applies anything left |
