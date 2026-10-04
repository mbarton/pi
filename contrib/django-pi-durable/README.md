# django-pi-durable

Reusable Django app that implements the pi-durable `Storage` contract (`packages/durable/src/types.ts`). A pi process
uses it through the TypeScript `HttpStorage` client in `client/`; Django code can read the same data directly through
`pi_durable.storage.DurableStorage`.

Status: sketch. It passes the pi-durable storage conformance suite on SQLite. PostgreSQL is untested.

## Layout

```
pi_durable/
  models.py    Store + one table per record type, mirroring storage/sqlite/migrations.ts
  storage.py   DurableStorage: port of SqliteStorage onto the ORM
  delta.py     Python port of Chord's apply(), used to materialize documents
  views.py     JSON-over-HTTP binding, one POST endpoint per Storage method
  access.py    Authorization hooks
  admin.py     Read-only admin
client/
  http-storage.ts       HttpStorage implements Storage over the HTTP API
  http-storage.test.ts  Runs registerStorageConformance() against `manage.py runserver`
testproject/            Minimal project for tests
```

## Design

**Stores.** `Storage` IDs are a single namespace per Session, and the root conversation is always ID 1. So every row
carries a `store` foreign key, and `(store, record_id)` is unique. One database can hold many Sessions. Deleting a
`Store` deletes its records.

**Commits.** `commit()` runs in `transaction.atomic()` and locks the `Store` row with `select_for_update()`. This
serializes commits of one store, so `next_seq` is strictly increasing, and all checks and writes in a batch commit or
roll back together.

**Records are JSON text, not `JSONField`.** PostgreSQL `jsonb` reorders object keys and rejects `\u0000`. Document
values must round-trip exactly, so records are stored as ASCII-escaped JSON text. Indexed string columns (`kind`,
`key_value`, `request_id`) are JSON-encoded for the same reason, as in the SQLite backend. The admin therefore shows
`"kind"` with quotes.

**Documents are materialized on the server.** A document is stored as a base checkpoint followed by Chord delta
batches. `document()` replays deltas over the newest base at or before the requested point. Example:

```
seq 4  base   {"count": 1}
seq 5  delta  [["s", ["count"], 2]]
seq 6  delta  [["s", ["count"], 3]]
document(id, 5)        -> {"count": 2}, deltasSinceBase 1
document(id, "current") -> {"count": 3}, deltasSinceBase 2
```

Replay must happen on the server because `document.copy` (forking a conversation) copies the materialized source
inside the commit transaction. `delta.py` ports Chord's seven ops; keep it in sync with
`packages/chord/src/delta/index.ts`. Note that `["t", path, n]` counts UTF-16 code units, as JavaScript does.

**ID minting.** `mintId()` runs once per new record. To avoid a round trip each time, the client reserves blocks
(`reserveIds`, default 64 IDs) and mints locally. Unused IDs become gaps, which the contract permits.

## HTTP API

```
POST <prefix>/stores                  {"label"?: str}  -> 201 {"result": {"id": "<uuid>"}}
POST <prefix>/stores/<uuid>/<method>  {named args}     -> {"result": ...}
```

Methods: `reserveIds`, `commit`, `conversation`, `scanConversations`, `entry`, `findLatestHeadMarker`, `scanEntries`,
`task`, `scanTasks`, `submission`, `scanSubmissions`, `submissionByRequest`, `findDocument`, `document`,
`scanDocuments`. Arguments use the TypeScript parameter names. A `null` result means `undefined`.

Errors: `{"error": {"type": "rejected" | "error", "message": ...}}`, status 409 for `StorageRejected` and 400
otherwise. Messages match the TypeScript backends.

Requests must be `Content-Type: application/json`. Views are CSRF-exempt; a cross-site form cannot send JSON without a
CORS preflight.

## Installation

```python
INSTALLED_APPS = [..., "pi_durable"]

urlpatterns = [..., path("durable/", include("pi_durable.urls"))]

# Optional; defaults: authenticated users create stores they own and access only those (superusers: all).
PI_DURABLE = {
    "CAN_CREATE_STORE": "myproject.durable.can_create_store",  # (request) -> bool
    "CAN_ACCESS_STORE": "myproject.durable.can_access_store",  # (request, store) -> bool
}
```

Authentication is the host project's job (session, token middleware, mTLS). Pass credentials through
`HttpStorageOptions.headers`.

```ts
const storage = await HttpStorage.create({ baseUrl: "https://example.com/durable/", headers: { Authorization } });
// later: HttpStorage.open(options, storage.id)
```

## Tests

```sh
pip install "Django>=4.2"
python manage.py test pi_durable

# Conformance suite against a live dev server (from this directory, after `npm ci --ignore-scripts` at the repo root)
cd client && PI_DURABLE_PYTHON=python3 node ../../../node_modules/vitest/dist/cli.js --run --config vitest.config.ts
```

## Open questions

- PostgreSQL: run the conformance suite against it. `document()` reads the base and its delta tail in one statement so
  `READ COMMITTED` cannot split them, but this is untested.
- Several pi processes writing one store: commits are serialized, but the Session above `Storage` assumes it is the
  only writer. A lease or fencing token on `Store` would make that explicit.
- Live updates: `Session` watches are in-process. Other readers (dashboards) would need a change feed, e.g. polling
  by `commit_seq`, or `LISTEN/NOTIFY`.
- Reads round-trip through `json.loads`/`json.dumps`. Splicing stored JSON text directly into responses would avoid
  that for large transcripts.
- Retention: no pruning of retired documents or old rewindable revisions yet.
