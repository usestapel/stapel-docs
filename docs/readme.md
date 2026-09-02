## What this is

Google-Drive-style **workspace documents**: a folder tree, documents that are
each ONE entity with a `type` from an open registry (`txt` / `md` / `csv` /
opaque `file` built in), a journal + revision versioning substrate, trash with
irreversible purge, and registry-driven export (PDF built in).

The versioning substrate is decided for both collaboration disciplines:
**snapshot** types save whole states under optimistic lock (`If-Match` carries
the client's `head_seq` — v1's editing model), **crdt** types accumulate an
append-only update journal between snapshots with chat-pattern replay/resync.
Which discipline applies is a property of the *type*, not the request.

Object storage is content-addressed and goes through a swappable seam
(`STORAGE`); the library is **body-blind** — the storage substrate never
parses a document body, only a type's own `text_extractor` may.

## Quick start

The base install rides Django's `default_storage`; add extras for the
boto3 S3/MinIO backend, the PDF exporter and image thumbnails:

```bash
pip install "stapel-docs[s3,pdf,thumbnails]"
```

```python
INSTALLED_APPS = [
    # ...
    "stapel_docs",
]

# urls.py
path("docs/", include("stapel_docs.urls"))   # -> /docs/api/v1/...
```

Authorization asks the `workspaces.check_capability` comm Function
(fail-closed, deny-by-default) — install stapel-workspaces or provide that
Function for any HTTP request to be allowed.

## An open type registry, not an enum

```python
STAPEL_DOCS = {
    # Add or replace document types ({slug: dotted-path | None removes}):
    "DOC_TYPES": {"sheet": "myproject.docs.SHEET_SPEC"},
    # Add export formats over the built-in pdf:
    "EXPORTERS": {"docx": "myproject.docs.DocxExporter"},
    # Event-driven ingest without writing a subscriber:
    "INGEST": {"meeting.summarized": "myproject.docs.map_summary"},
    # Swap the object store:
    "STORAGE": "stapel_docs.storage.S3Backend",
}
```

A type whose spec vanishes from the registry degrades to `file` behavior —
read-only, never unreadable: revisions still list, snapshots still download,
trash/purge/export still work.

## Export

`?format=pdf` renders `txt` verbatim, `csv` as a bordered grid, and `md`
**parsed** — headings, bold/italic, bulleted and numbered lists, fenced code
in monospace, tables, clickable links. Cyrillic (and everything else DejaVu
covers) holds throughout, code blocks included, because the fonts ship in the
wheel. Pure python: fpdf2 + Python-Markdown, no WeasyPrint and so no system
pango/cairo to install. A document body is user input on its way to an HTML
renderer, so it is sanitized to an allowlist first — images are dropped
before the renderer can fetch them, scripts and styles lose their content,
and only `http(s)`/`mailto` links become annotations.

## Ingest

Product glue dumps content in with one comm call — no HTTP, no import:

```python
call("docs.create_document", {
    "workspace_id": ws_id, "type": "md", "title": "Weekly sync",
    "body": summary_text, "folder_path": "/Meetings/2026-08",
})
```

`folder_path` materializes folders idempotently; an unknown `type` refuses
loudly so content never silently lands under a mistyped slug.

## The drive surfaces

Four per-user views over the same corpus, all behind the one authorization
choke point:

- **Starred** — `POST`/`DELETE /documents/<id>/star` and the folder twin,
  `GET /starred`. Idempotent both ways (204 whatever the previous state was)
  and gated by `docs.view`: a star is a bookmark, not an edit. Every folder
  and document envelope carries `is_starred` — `null`, not `false`, when the
  request has no user, because "not applicable" is a third answer.
- **Recents** — `GET /recents`. Written by the service layer on content read,
  download-URL issuance and accepted save; a rejected save records nothing.
  Capped by `RECENTS_MAX_PER_USER` and trimmed oldest-first on write.
- **Search** — `GET /search?workspace_id=&q=`. Workspace-scoped
  case-insensitive substring over live folder names and document titles, each
  hit carrying its `kind` and a server-built breadcrumb. A missing `q` is a
  400, not a free full-workspace listing. `?q=` also filters the documents
  listing for the in-folder case.
- **Thumbnails** — `GET /documents/<id>/thumbnail?tier=` for `image/*` file
  documents, tiers `160` and `480`. Rendered server-side with Pillow and
  cached under the document's own storage prefix, so a purge takes the
  previews with it. Without the `[thumbnails]` extra the endpoint answers 503
  and a client falls back to a type icon.

## Usage metering

`docs.usage` reports `{bytes_live, bytes_trash, bytes_total, documents,
folders, by_type}` for one workspace. `bytes_total` is the same sum the 507
quota refuses against, so a meter and a refusal can never disagree. Composing
an entitlement ceiling out of it (`billing.check_entitlement`) is the host's
glue — docs owns the measurement, never the price. The call carries its own
authority exactly like `docs.create_document`, one capability lower
(`docs.view`).

## Sharing (v1: closed by default)

The sharing axis (`SHARING`: whitelist / link modes) ships its config surface
with **closed defaults** — v1 implements exactly the immutable workspace
baseline, and opening any sharing knob before the mechanism exists is a loud
system-check error (`stapel_docs.E010-E013`), never a silent no-op.

## Settings

All configuration lives in the `STAPEL_DOCS` namespace (dict setting, flat
setting, or env var — resolved lazily). Full table in
[CONFIG.MD](https://github.com/usestapel/stapel-docs/blob/main/CONFIG.MD);
seam semantics in
[MODULE.md](https://github.com/usestapel/stapel-docs/blob/main/MODULE.md).
Highlights: `STORAGE`, `DOC_TYPES`, `EXPORTERS`, `INGEST`, `REPLAY_WINDOW`,
`AUTO_REVISION_INTERVAL_SECONDS`, `TRASH_RETENTION_DAYS`, `RECENTS_MAX_PER_USER`,
`SEARCH_MAX_RESULTS`, `SHARING`. Thumbnail tiers are deliberately a fixed
constant, not a setting: the tier is part of a URL clients cache against.

## comm surface

| Kind | Name | Contract |
|---|---|---|
| Function (provides) | `docs.create_document` | `schemas/functions/docs.create_document.json` |
| Function (provides) | `docs.usage` | `schemas/functions/docs.usage.json` |
| Action (emit) | `document.created`, `document.updated`, `document.deleted`, `document.storage_changed` | `schemas/emits/*.json` |
| Action (consume) | `user.deleted` | GDPR anonymize (authorship nulled, content survives) |
| Action (consume) | `user.merged` | authorship re-parented to the surviving account (the opposite instruction to erasure); stars and recents folded on collision |
| Function (call) | `workspaces.check_capability` | provided by stapel-workspaces |

## Operations

```bash
python manage.py docs_purge_expired   # purge trash older than TRASH_RETENTION_DAYS
```

## Development

```bash
pip install -e . && pip install pytest pytest-django ruff jsonschema djangorestframework
./setup-hooks.sh
pytest tests/
```
