# Deploying the review app `{{app_name}}`

This folder is a complete Databricks app: the review app of `forge-structured-doc-extraction`, generated in a notebook (module 07). Reviewers use it to approve or correct the extracted values that landed in the review queue. Databricks Apps installs its packages (`npm install`) and builds it (`npm run build`) each time it deploys, so nothing needs installing in the workspace.

**What it uses:**
- the Lakebase database `{{database}}`, on branch `{{branch}}`:
  - it reads the review queue, `{{queue_postgres}}`;
  - it writes the reviewers' decisions to `{{decisions_postgres}}`, which it creates on its first start;
- the documents volume `{{volume}}`, read as each reviewer, so the document pane shows only files they may read.

## 1. Create the app (once)

1. Open **Compute**, then the **Apps** tab, and choose **Create app**, then **Create a custom app**.
2. Name it `{{app_name}}`.
3. Add two **App resources**. Give each the resource key shown: `app.yaml` finds them by key.

   | Resource | Resource key | Value | Permission |
   |---|---|---|---|
   | Lakebase (Postgres) database | `postgres` | branch `{{branch}}`, database `{{database}}` | Can connect and create |
   | Unity Catalog volume | `files` | `{{volume}}` | Can read and write, as the AppKit files plugin asks; the app only reads |

4. Under **User authorization**, add the scope `files.files`, so the document pane reads files as the reviewer.
5. Create the app. Creating it doesn't deploy it yet.

## 2. Deploy it

1. On the app's page, choose **Deploy**. If it was deployed before, use the arrow next to **Deploy** and **Deploy using a different source**.
2. In **Create deployment**, choose **From workspace**, and set **Source code path** to `{{app_folder}}`.
3. Deploy. The first build takes a few minutes; the app's **Logs** show `npm install` and `npm run build`.

To deploy a change later, such as a new version of these files after `render.py` runs again, deploy again from the same folder.

## 3. Let the app read the queue (once, after the first deploy)

The synced table owns the queue's Postgres schema, so the app's service principal can't read it until the Lakebase project's owner grants it. Copy the app's service principal ID from its **Authorization** tab, or ask Genie Code for it. Then, as the owner of the project `{{project}}`, open its **SQL editor** on the database `databricks_postgres`, and run:

```sql
GRANT USAGE ON SCHEMA "{{schema}}" TO "<service principal ID>";
GRANT SELECT ON "{{schema}}"."{{prefix}}_review_queue" TO "<service principal ID>";
```

## 4. Share it with the reviewers

On the app's **Permissions**, give **Can use** to the reviewers only. Anyone who can open the app sees every queued value: synced tables don't carry Unity Catalog row filters or column masks. Each reviewer also needs `READ VOLUME` on `{{volume}}` to see the documents.

## Deploying from a computer instead

With the Databricks CLI, export this folder (`databricks workspace export-dir {{app_folder}} {{app_name}}`), then from inside it run `databricks bundle deploy` and `databricks bundle run app`. Its `databricks.yml` defines the app, its two resources, and the `files.files` scope, so steps 1 and 2 happen together. Steps 3 and 4 are the same.

If something doesn't work, module 07's Troubleshooting table in the skill lists the usual causes.
