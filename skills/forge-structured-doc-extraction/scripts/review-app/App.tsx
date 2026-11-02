// Client of the forge-structured-doc-extraction review app (module 07): documents with low-confidence values on the
// left, the document in the middle, and its values on the right, each approved or corrected by the reviewer.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Label,
  Textarea,
} from '@databricks/appkit-ui/react';

interface Summary {
  fields: number;
  open: number;
  approved: number;
  corrected: number;
  reviewer: string | null;
}

interface ReviewDocument {
  path: string;
  fileName: string;
  fields: number;
  open: number;
  minConfidence: number | null;
}

interface ReviewField {
  reviewId: string;
  segmentId: number;
  docType: string;
  field: string;
  fieldType: string;
  description: string;
  labels: string[];
  pages: number[];
  aiValue: string | null;
  confidence: number | null;
  decision: string | null;
  value: string | null;
  comment: string | null;
  reviewedBy: string | null;
  reviewedAt: string | null;
  rowVersion: number;
}

class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error = body && typeof body === 'object' && 'error' in body ? body.error : null;
    throw new ApiError(typeof error === 'string' ? error : `Request failed (${response.status})`, response.status);
  }
  return body as T;
}

/** The files plugin's URL for a document in the volume (paths are relative to the volume root). */
function documentUrl(path: string, page: number | null): string | null {
  const start = path.indexOf('/Volumes/');
  const parts = start < 0 ? [] : path.slice(start + '/Volumes/'.length).split('/').filter(Boolean);
  if (parts.length < 4) return null;
  const url = `/api/files/files/raw?${new URLSearchParams({ path: parts.slice(3).join('/') })}`;
  return page ? `${url}#page=${page}` : url;
}

const show = (value: string | null) => (value === null ? 'Not in the document' : value);
const percent = (value: number | null) => (value === null ? 'no score' : `${Math.round(value * 100)}% confidence`);

function FieldCard({ field, onSaved, onPage }: { field: ReviewField; onSaved: () => void; onPage: (page: number) => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(field.value ?? field.aiValue ?? '');
  const [absent, setAbsent] = useState(false);
  const [comment, setComment] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [requestId, setRequestId] = useState<string | null>(null); // reused on a retry, so it's saved once

  const submit = async (action: 'approve' | 'correct') => {
    const id = requestId ?? crypto.randomUUID();
    setRequestId(id);
    setSaving(true);
    setError(null);
    try {
      await api('/api/review/decisions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          reviewId: field.reviewId,
          expectedRowVersion: field.rowVersion,
          action,
          value: absent ? null : value,
          comment,
          decisionRequestId: id,
        }),
      });
      onSaved(); // the reloaded field has a new rowVersion, which remounts this card
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'The decision was not saved');
      if (failure instanceof ApiError && failure.status !== 500) setRequestId(null);
    } finally {
      setSaving(false);
    }
  };

  const changed = () => setRequestId(null);
  const decided = Boolean(field.decision) && !editing;

  return (
    <Card className="shadow-none">
      <CardHeader className="space-y-1 p-4">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="break-words text-sm">{field.field}</CardTitle>
          <Badge variant={decided ? 'secondary' : 'outline'}>{field.decision ?? 'open'}</Badge>
        </div>
        <p className="text-xs text-muted-foreground">
          {field.docType} · {field.fieldType} ·{' '}
          {field.pages.length ? (
            <button type="button" className="underline" onClick={() => onPage(field.pages[0])}>
              page {field.pages.join(', ')}
            </button>
          ) : (
            'no page'
          )}
        </p>
        {field.description ? <p className="text-xs text-muted-foreground">{field.description}</p> : null}
      </CardHeader>
      <CardContent className="space-y-3 p-4 pt-0">
        <div className="rounded-md border bg-muted/40 p-3 text-sm">
          <div className="mb-1 flex justify-between text-xs text-muted-foreground">
            <span>Extracted</span>
            <span>{percent(field.confidence)}</span>
          </div>
          <p className="whitespace-pre-wrap break-words font-mono">{show(field.aiValue)}</p>
        </div>

        {decided ? (
          <div className="space-y-2 text-xs">
            <p>
              <span className="font-medium">{field.decision === 'approved' ? 'Approved' : 'Corrected to'}</span>
              {field.decision === 'corrected' ? <span className="font-mono"> {show(field.value)}</span> : null}
            </p>
            <p className="text-muted-foreground">
              {field.reviewedBy} · {field.reviewedAt ? new Date(field.reviewedAt).toLocaleString() : ''}
              {field.comment ? ` · ${field.comment}` : ''}
            </p>
            <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
              Change
            </Button>
          </div>
        ) : (
          <>
            {editing ? (
              <div className="space-y-2">
                <Label htmlFor={`value-${field.reviewId}`}>Correct value</Label>
                {field.fieldType === 'enum' || field.fieldType === 'boolean' ? (
                  <select
                    id={`value-${field.reviewId}`}
                    className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                    value={value}
                    disabled={absent}
                    onChange={(event) => {
                      setValue(event.target.value);
                      changed();
                    }}
                  >
                    <option value="">Choose…</option>
                    {(field.fieldType === 'boolean' ? ['true', 'false'] : field.labels).map((label) => (
                      <option key={label} value={label}>
                        {label}
                      </option>
                    ))}
                  </select>
                ) : (
                  <Input
                    id={`value-${field.reviewId}`}
                    value={value}
                    disabled={absent}
                    onChange={(event) => {
                      setValue(event.target.value);
                      changed();
                    }}
                  />
                )}
                <label className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={absent}
                    onChange={(event) => {
                      setAbsent(event.target.checked);
                      changed();
                    }}
                  />
                  Not in the document
                </label>
                <Label htmlFor={`comment-${field.reviewId}`}>What was wrong (required)</Label>
                <Textarea
                  id={`comment-${field.reviewId}`}
                  value={comment}
                  onChange={(event) => {
                    setComment(event.target.value);
                    changed();
                  }}
                />
              </div>
            ) : null}
            {error ? (
              <Alert variant="destructive">
                <AlertTitle>Not saved</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            ) : null}
            <div className="flex flex-wrap gap-2">
              {editing ? (
                <>
                  <Button disabled={saving} onClick={() => void submit('correct')}>
                    Save correction
                  </Button>
                  <Button variant="ghost" disabled={saving} onClick={() => setEditing(false)}>
                    Cancel
                  </Button>
                </>
              ) : (
                <>
                  <Button disabled={saving} onClick={() => void submit('approve')}>
                    Approve
                  </Button>
                  <Button variant="outline" disabled={saving} onClick={() => setEditing(true)}>
                    Correct
                  </Button>
                </>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

export default function App() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [documents, setDocuments] = useState<ReviewDocument[]>([]);
  const [status, setStatus] = useState<'open' | 'all'>('open');
  const [selected, setSelected] = useState<string | null>(null);
  const [fields, setFields] = useState<ReviewField[]>([]);
  const [page, setPage] = useState<number | null>(null);
  const [search, setSearch] = useState('');
  const [error, setError] = useState<string | null>(null);

  const loadQueue = useCallback(async () => {
    try {
      const [nextSummary, nextDocuments] = await Promise.all([
        api<Summary>('/api/review/summary'),
        api<ReviewDocument[]>(`/api/review/documents?status=${status}`),
      ]);
      setSummary(nextSummary);
      setDocuments(nextDocuments);
      setSelected((current) => current ?? nextDocuments[0]?.path ?? null);
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Could not load the review queue');
    }
  }, [status]);

  const loadFields = useCallback(async (path: string) => {
    try {
      setFields(await api<ReviewField[]>(`/api/review/fields?${new URLSearchParams({ path })}`));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Could not load the fields');
    }
  }, []);

  useEffect(() => {
    void loadQueue();
  }, [loadQueue]);

  useEffect(() => {
    setPage(null);
    if (selected) void loadFields(selected);
    else setFields([]);
  }, [selected, loadFields]);

  const saved = () => {
    if (selected) void loadFields(selected);
    void loadQueue();
  };

  const shown = documents.filter((document) => document.fileName.toLowerCase().includes(search.trim().toLowerCase()));
  const url = selected ? documentUrl(selected, page) : null;

  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3">
        <div>
          <h1 className="text-lg font-semibold">Extraction review</h1>
          <p className="text-xs text-muted-foreground">
            Approve or correct each value. Decisions reach the extracted tables within a few minutes.
          </p>
        </div>
        <div className="flex items-center gap-3 text-xs">
          {summary ? (
            <>
              <Badge>{summary.open} open</Badge>
              <span className="text-muted-foreground">
                {summary.approved} approved · {summary.corrected} corrected
              </span>
              <span>{summary.reviewer ?? 'unknown reviewer'}</span>
            </>
          ) : null}
        </div>
      </header>

      {error ? (
        <Alert variant="destructive" className="m-3">
          <AlertTitle>Review data unavailable</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      <main className="grid min-h-0 flex-1 grid-cols-[16rem_minmax(24rem,1fr)_26rem]">
        <section className="flex min-h-0 flex-col border-r" aria-label="Documents">
          <div className="space-y-2 border-b p-3">
            <Input placeholder="Search documents" value={search} onChange={(event) => setSearch(event.target.value)} />
            <div className="flex gap-2">
              <Button size="sm" variant={status === 'open' ? 'default' : 'outline'} onClick={() => setStatus('open')}>
                Open
              </Button>
              <Button size="sm" variant={status === 'all' ? 'default' : 'outline'} onClick={() => setStatus('all')}>
                All
              </Button>
              <Button size="sm" variant="ghost" onClick={() => void loadQueue()}>
                Refresh
              </Button>
            </div>
          </div>
          <div className="min-h-0 flex-1 space-y-1 overflow-auto p-2">
            {shown.length === 0 ? <p className="p-2 text-sm text-muted-foreground">Nothing to review.</p> : null}
            {shown.map((document) => (
              <button
                key={document.path}
                type="button"
                className={`w-full rounded-md border p-2 text-left text-sm hover:bg-muted ${
                  document.path === selected ? 'border-primary bg-muted' : 'border-transparent'
                }`}
                onClick={() => setSelected(document.path)}
              >
                <p className="truncate font-medium" title={document.path}>
                  {document.fileName}
                </p>
                <p className="text-xs text-muted-foreground">
                  {document.open} open of {document.fields}
                </p>
              </button>
            ))}
          </div>
        </section>

        <section className="min-h-0 bg-muted" aria-label="Document">
          {url ? (
            <iframe key={url} className="h-full w-full" src={url} title={selected ?? 'document'} />
          ) : (
            <p className="p-6 text-sm text-muted-foreground">
              {selected ? `${selected} isn't in a Unity Catalog volume.` : 'Choose a document.'}
            </p>
          )}
        </section>

        <section className="min-h-0 space-y-3 overflow-auto border-l p-3" aria-label="Values">
          {fields.map((field) => (
            <FieldCard key={`${field.reviewId}:${field.rowVersion}`} field={field} onSaved={saved} onPage={setPage} />
          ))}
        </section>
      </main>
    </div>
  );
}
