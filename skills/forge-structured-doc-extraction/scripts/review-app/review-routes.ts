// Review routes of the forge-structured-doc-extraction review app (module 07). The app reads the review queue from
// a Lakebase synced table (read-only: writes would corrupt the sync) and writes each decision to its own table,
// which Lakehouse Sync copies to Unity Catalog for the extraction job to apply. Nothing here touches Delta.
// render.py sets REVIEW_QUEUE_TABLE and REVIEW_DECISIONS_TABLE in app.yaml.
import type { Application, Request } from 'express';
import { z } from 'zod';

type Rows = { rows: Record<string, unknown>[] };

interface ReviewClient {
  query(text: string, params?: unknown[]): Promise<Rows>;
  release(): void;
}

/** The parts of the AppKit handle these routes use. */
interface ReviewAppKit {
  lakebase: {
    query(text: string, params?: unknown[]): Promise<Rows>;
    pool: { connect(): Promise<ReviewClient> };
  };
  server: { extend(register: (app: Application) => void): void };
}

const Decision = z
  .object({
    reviewId: z.string().min(1).max(128),
    expectedRowVersion: z.number().int().min(0),
    action: z.enum(['approve', 'correct']),
    value: z.string().max(10_000).nullable().optional(), // a correction; null means the document has no value
    comment: z.string().max(4_000).optional(),
    decisionRequestId: z.uuid(), // the same id on a retry, so a retried request isn't saved twice
  })
  .refine((body) => body.action === 'approve' || Boolean(body.comment?.trim()), {
    message: 'Add a note that says what was wrong',
    path: ['comment'],
  });

class RequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** `schema.table` from the environment, quoted for Postgres. */
function tableName(variable: string): string {
  const parts = (process.env[variable] ?? '').split('.');
  if (parts.length !== 2 || !parts.every((part) => /^[A-Za-z0-9_]+$/.test(part))) {
    throw new Error(`${variable} must be <schema>.<table>; render.py writes it into app.yaml`);
  }
  return parts.map((part) => `"${part}"`).join('.');
}

/** Reviewer identity from the Databricks Apps proxy. */
function reviewer(request: Request): string | null {
  const actor = request.header('x-forwarded-email') ?? request.header('x-forwarded-user') ?? null;
  return actor ?? (process.env.NODE_ENV === 'development' ? 'local-reviewer' : null);
}

/** A corrected value as text, in the form <prefix>_fields uses, checked against the field's type. */
export function normalizeValue(input: string | null | undefined, fieldType: string, labels: string[]): string | null {
  if (input === null || input === undefined) return null;
  const text = input.trim();
  if (!text) throw new RequestError('Enter a value, or choose "Not in the document"', 400);
  if (fieldType === 'number' || fieldType === 'integer') {
    const number = Number(text.replace(/[,\s]/g, ''));
    if (!Number.isFinite(number)) throw new RequestError('Enter a number, such as 1884.50', 400);
    if (fieldType === 'integer' && !Number.isInteger(number)) throw new RequestError('Enter a whole number', 400);
    return String(number);
  }
  if (fieldType === 'boolean') {
    const answer = text.toLowerCase();
    if (answer === 'true' || answer === 'yes') return 'true';
    if (answer === 'false' || answer === 'no') return 'false';
    throw new RequestError('Enter true or false', 400);
  }
  if (fieldType === 'enum') {
    const label = labels.find((candidate) => candidate.toLowerCase() === text.toLowerCase());
    if (!label) throw new RequestError(`Choose one of: ${labels.join(', ')}`, 400);
    return label;
  }
  return text;
}

const text = (value: unknown): string | null => (value === null || value === undefined ? null : String(value));
const time = (value: unknown): string | null => (value instanceof Date ? value.toISOString() : text(value));
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

function fieldRow(row: Record<string, unknown>) {
  return {
    reviewId: String(row.review_id),
    segmentId: Number(row.segment_id),
    docType: text(row.doc_type) ?? '',
    field: String(row.field),
    fieldType: text(row.field_type) ?? 'string',
    description: text(row.field_description) ?? '',
    labels: list(row.enum_labels).map(String),
    pages: list(row.page_numbers).map(Number),
    aiValue: text(row.ai_value),
    confidence: row.confidence === null ? null : Number(row.confidence),
    decision: text(row.decision),
    value: text(row.value),
    comment: text(row.comment),
    reviewedBy: text(row.reviewed_by),
    reviewedAt: time(row.reviewed_at),
    rowVersion: Number(row.row_version ?? 0),
  };
}

/** Create the app's decisions table. The app's service principal owns the schema; Lakehouse Sync needs REPLICA IDENTITY FULL. */
export async function setupReviewTables(appkit: ReviewAppKit): Promise<void> {
  const decisions = tableName('REVIEW_DECISIONS_TABLE');
  try {
    await appkit.lakebase.query(`CREATE SCHEMA IF NOT EXISTS ${decisions.split('.')[0]}`);
    // Columns must match DECISION_COLUMNS in extract.py, which reads them from the Lakehouse Sync history.
    await appkit.lakebase.query(`
      CREATE TABLE IF NOT EXISTS ${decisions} (
        review_id TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        segment_id INTEGER NOT NULL,
        doc_type TEXT NOT NULL,
        field TEXT NOT NULL,
        parsed_at TIMESTAMPTZ NOT NULL,
        ai_value TEXT,
        decision TEXT NOT NULL CHECK (decision IN ('approved', 'corrected')),
        value TEXT,
        comment TEXT,
        reviewed_by TEXT NOT NULL,
        reviewed_at TIMESTAMPTZ NOT NULL,
        decision_request_id TEXT NOT NULL UNIQUE,
        row_version BIGINT NOT NULL CHECK (row_version > 0),
        CHECK (decision = 'approved' OR comment IS NOT NULL)
      )`);
    await appkit.lakebase.query(`ALTER TABLE ${decisions} REPLICA IDENTITY FULL`);
  } catch (error) {
    console.error('[review] Could not create the decisions table; decisions will fail until this is fixed', error);
  }
}

export function registerReviewRoutes(appkit: ReviewAppKit): void {
  const queue = tableName('REVIEW_QUEUE_TABLE');
  const decisions = tableName('REVIEW_DECISIONS_TABLE');
  const reviewed = `${queue} q LEFT JOIN ${decisions} d ON d.review_id = q.review_id`;

  appkit.server.extend((app) => {
    app.get('/api/review/summary', async (request, response) => {
      try {
        const { rows } = await appkit.lakebase.query(`
          SELECT count(*)::int AS fields,
                 (count(*) FILTER (WHERE d.review_id IS NULL))::int AS open,
                 (count(*) FILTER (WHERE d.decision = 'approved'))::int AS approved,
                 (count(*) FILTER (WHERE d.decision = 'corrected'))::int AS corrected
          FROM ${reviewed}`);
        response.json({ ...rows[0], reviewer: reviewer(request) });
      } catch (error) {
        console.error('[review] summary failed', error);
        response.status(500).json({ error: 'Could not read the review queue' });
      }
    });

    app.get('/api/review/documents', async (request, response) => {
      const status = request.query.status === 'all' ? 'all' : 'open';
      try {
        const { rows } = await appkit.lakebase.query(
          `SELECT q.path, min(q.file_name) AS file_name, count(*)::int AS fields,
                  (count(*) FILTER (WHERE d.review_id IS NULL))::int AS open,
                  min(q.confidence) AS min_confidence
           FROM ${reviewed}
           GROUP BY q.path
           HAVING $1 = 'all' OR count(*) FILTER (WHERE d.review_id IS NULL) > 0
           ORDER BY open DESC, min_confidence ASC NULLS FIRST, q.path
           LIMIT 200`,
          [status],
        );
        response.json(
          rows.map((row) => ({
            path: String(row.path),
            fileName: text(row.file_name) ?? String(row.path),
            fields: Number(row.fields),
            open: Number(row.open),
            minConfidence: row.min_confidence === null ? null : Number(row.min_confidence),
          })),
        );
      } catch (error) {
        console.error('[review] documents failed', error);
        response.status(500).json({ error: 'Could not read the review queue' });
      }
    });

    app.get('/api/review/fields', async (request, response) => {
      const path = typeof request.query.path === 'string' ? request.query.path : '';
      if (!path || path.length > 2048) {
        response.status(400).json({ error: 'Pass the document path' });
        return;
      }
      try {
        const { rows } = await appkit.lakebase.query(
          `SELECT q.review_id, q.segment_id, q.doc_type, q.field, q.field_type, q.field_description, q.enum_labels,
                  q.page_numbers, q.ai_value, q.confidence, d.decision, d.value, d.comment, d.reviewed_by,
                  d.reviewed_at, COALESCE(d.row_version, 0)::int AS row_version
           FROM ${reviewed}
           WHERE q.path = $1
           ORDER BY q.segment_id, q.confidence ASC NULLS FIRST, q.field`,
          [path],
        );
        response.json(rows.map(fieldRow));
      } catch (error) {
        console.error('[review] fields failed', error);
        response.status(500).json({ error: 'Could not read the fields' });
      }
    });

    app.post('/api/review/decisions', async (request, response) => {
      const actor = reviewer(request);
      if (!actor) {
        response.status(401).json({ error: 'No reviewer identity' });
        return;
      }
      const parsed = Decision.safeParse(request.body);
      if (!parsed.success) {
        response.status(400).json({ error: parsed.error.issues.map((issue) => issue.message).join('; ') });
        return;
      }
      const body = parsed.data;
      const client = await appkit.lakebase.pool.connect();
      try {
        await client.query('BEGIN');
        const candidate = (
          await client.query(`SELECT field_type, enum_labels FROM ${queue} WHERE review_id = $1`, [body.reviewId])
        ).rows[0];
        if (!candidate) throw new RequestError('This value is no longer in the review queue. Refresh.', 404);
        const value =
          body.action === 'correct'
            ? normalizeValue(body.value, text(candidate.field_type) ?? 'string', list(candidate.enum_labels).map(String))
            : null;

        const current = (
          await client.query(
            `SELECT row_version::int AS row_version, decision_request_id FROM ${decisions} WHERE review_id = $1 FOR UPDATE`,
            [body.reviewId],
          )
        ).rows[0];
        if (current && current.decision_request_id === body.decisionRequestId) {
          await client.query('COMMIT'); // a retry of a request that was already saved
          response.json({ reviewId: body.reviewId, rowVersion: Number(current.row_version) });
          return;
        }
        const version = Number(current?.row_version ?? 0);
        if (version !== body.expectedRowVersion) {
          throw new RequestError('Someone else decided this value since you opened it. Refresh.', 409);
        }

        // The approved value and the parse it belongs to come from the queue, not the request.
        const saved = await client.query(
          `INSERT INTO ${decisions} AS d (review_id, path, segment_id, doc_type, field, parsed_at, ai_value, decision,
             value, comment, reviewed_by, reviewed_at, decision_request_id, row_version)
           SELECT q.review_id, q.path, q.segment_id, q.doc_type, q.field, q.parsed_at, q.ai_value, $2::text,
             CASE WHEN $2::text = 'approved' THEN q.ai_value ELSE $3::text END, NULLIF($4::text, ''), $5::text, NOW(),
             $6::text, $7::bigint
           FROM ${queue} q WHERE q.review_id = $1
           ON CONFLICT (review_id) DO UPDATE SET ai_value = EXCLUDED.ai_value, decision = EXCLUDED.decision,
             value = EXCLUDED.value, comment = EXCLUDED.comment, reviewed_by = EXCLUDED.reviewed_by,
             reviewed_at = EXCLUDED.reviewed_at, decision_request_id = EXCLUDED.decision_request_id,
             row_version = EXCLUDED.row_version
           WHERE d.row_version = $8::bigint
           RETURNING d.row_version::int AS row_version`,
          [
            body.reviewId,
            body.action === 'approve' ? 'approved' : 'corrected',
            value,
            body.comment?.trim() ?? '',
            actor,
            body.decisionRequestId,
            version + 1,
            version,
          ],
        );
        if (!saved.rows[0]) throw new RequestError('Someone else decided this value since you opened it. Refresh.', 409);
        await client.query('COMMIT');
        response.json({ reviewId: body.reviewId, rowVersion: Number(saved.rows[0].row_version) });
      } catch (error) {
        await client.query('ROLLBACK').catch(() => undefined);
        if (error instanceof RequestError) {
          response.status(error.status).json({ error: error.message });
        } else {
          console.error('[review] decision failed', error);
          response.status(500).json({ error: 'The decision was not saved. Try again.' });
        }
      } finally {
        client.release();
      }
    });
  });
}
