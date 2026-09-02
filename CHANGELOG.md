# Changelog

All notable changes to stapel-docs are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [Unreleased]

## [0.6.0] — 2026-09-02

### Added — the sharing mechanism: grant sources over an immutable baseline

`tasks/sharing-axis-design.md`, implemented as ratified;
`tasks/stapel-drive-spec.md` §3.5 for the landing notes. Sharing is not a
model this module chose — it is a set of **switchable grant sources** layered
over a workspace baseline that never changes. The axis was declared closed in
0.2.0 with its config surface live and its mechanism absent; this release
fills the mechanism in and leaves the surface exactly as closed as it was.

- **`DocumentAccess`** — the whitelist row. Two subject kinds in one table,
  because both mean the same thing (named principals, no bearer secret) and
  differ only in how membership is computed: `user` matches an account by id,
  `ref` names an EXTERNAL container (`chat:conversation:<id>`) whose
  membership a host resolver answers by point query. docs never copies
  another module's membership and never imports it — the chat case is a
  whitelist with a different lookup, not a fourth mode. Exactly-one-subject is
  a `CheckConstraint`; one grant per subject per document is a pair of
  partial uniques (`""` does equal `""` in SQL, unlike NULL).
- **`DocumentLink`** — the bearer link, the `WorkspaceInvitation` canon copied
  in shape rather than reinvented: `@access.secret`, unguessable
  `secrets.token_urlsafe(32)` token, MANDATORY `expires_at`, derived status
  where **revoked beats expired beats active**, `first_redeemed_at` stamped
  once. One difference in substance: a link is not a mandate. It is never
  "accepted", it creates no membership, and it is re-checked on every single
  presentation.
- **`authorize()` step 2** — the union of enabled sources with the maximum
  level. Each source is an independent sufficient reason; no source can say
  "no", which is why two enabled modes cannot disagree, why disabling one can
  never open anything, and why the baseline can never be configured away.
- **Endpoints** — `GET`/`POST /documents/<id>/access` +
  `DELETE …/access/<access_id>` (gated by `docs.share.whitelist`),
  `GET`/`POST /documents/<id>/links` + `DELETE …/links/<link_id>` (gated by
  `docs.share.link`; revocation additionally open to `docs.manage`, because
  taking access away must never be the thing nobody in the room is allowed to
  do), and the bearer read path `GET /shared/<token>` + `/content` +
  `/download`.
- **Events** — `document.share.granted` / `revoked` / `link_created` /
  `link_revoked` / `link_redeemed` (first redemption only), schemas under
  `schemas/emits/`, emitted inside the mutating transaction. **No payload
  carries a token**: an event is copied into an outbox, a broker, a log
  aggregator and somebody's dashboard, and a bearer secret that travels that
  far has been leaked by its own audit trail.
- **`IMPLEMENTED_SHARING_MODES = ("whitelist", "link")`** — E011 stops firing
  for them and stays for the next mode somebody configures before it is
  built (proven by a test that shrinks the list rather than deleting itself).

### Security — what the axis refuses, and why each refusal has a test

- **`manage` is never grantable by any share source.** A principal shared
  into a document may read and write the body and can still never delete it,
  move it, or widen access to it. This is the anti-escalation invariant the
  whole axis rests on, and it is enforced by having no reachable grant path
  for the action at all rather than by a check somebody could forget.
- **An anonymous presenter never writes**, whatever level their link carries.
  The journal and the revision history are attributed by design; an
  authorless edit is vandalism with no subject to name, and the one
  combination the axis forbids forever is "edit AND anonymous".
- **A link dies when its creator loses `docs.share.link`** — checked live on
  every presentation. The asymmetry with whitelist is deliberate: a whitelist
  row is enumerable and an admin can strike it, but a bearer token in unknown
  hands whose sponsor has left is the leak itself.
- **An outage is not a verdict.** Both the baseline and the sponsor check
  answer 503 on an unreachable workspaces service. A user locked out by an
  outage and a user correctly refused must not receive the same answer.
- **Resolvers fail closed on both boundaries**: an unregistered ref kind is
  refused at MINT (a row that could only ever deny is never stored), and an
  unknown kind, an unimportable path or a raising resolver denies at READ.
  Answers are cached ~30 s — the middle between a copied membership that
  never learns about a revocation and no cache at all.
- **The bearer path is not an oracle.** A dead token and a token that never
  existed get the identical 404; only a missing session gets a 401, because
  "sign in" is the one refusal that tells the holder of a good link what to
  do.
- **A disabled mode inerts its rows, it does not hide them.** They stop
  granting, they stay in the share sheet marked `suspended`, and minting into
  a disabled mode is refused rather than storing a grant nothing will read.
  An admin who cannot see an inert grant believes it was revoked.

### Changed

- **GDPR: sharing rows are the exception to "anonymize, not delete".** An
  account erasure DELETES the `DocumentAccess` rows naming that user as
  subject and REVOKES the links they sponsored — a standing permission about
  a person is not co-produced content and must not outlive them. Provenance
  (`granted_by`, `created_by`) is anonymized like any other authorship. The
  erasure receipt gains `access_grants` and `links` counts; document and
  workspace erasure take both tables with them through FK CASCADE.
- **`user.merged` carries grants over with collision folding to the HIGHER
  level.** Folding down would revoke access as a side effect of a merge — a
  silent loss on the one table where losing access is hardest to diagnose.
- `authorize()` gains `granted_level()` and `check_share_capability()`
  alongside it. The first exists so the presentation layer never re-derives
  "what may this bearer do" — a second answer to that question is how a share
  mode ships half-enforced. The second is deliberately outside `authorize()`'s
  action vocabulary: minting a grant is a workspace mandate, not a level on
  the document, and conflating them turns "shared with me" into "may share
  with others".
- llms.txt budget 7000 → 9000 (22 more called symbols, 7 more operations,
  7 more error keys). Raised deliberately, per the generator's own advice:
  shortening the intent lines of security gates is how a gate becomes
  something nobody can explain and therefore nobody adopts.

### Unchanged, on purpose

- **The shipped default is still `MODES: []`** — a document is visible to
  exactly its workspace until a host writes one line of settings. Turning
  sharing on is one line; turning it off afterwards is a set of links already
  sent.
- **E012 (`LINK["ANONYMOUS"]=True`) and E013 (`MAX_LEVEL` above `view`) still
  fail deploys.** The rule carries both branches and both are covered by
  tests, but the owner's §10 verdict governs what a DEPLOYMENT may switch on:
  anonymous links and edit-by-link need an owner decision, not a config
  override. This is the honest state and it is stated in `checks.py`,
  `CONFIG.MD` and `MODULE.md` rather than implied.
- **Per-workspace narrowing is not wired.** The axis names
  `Workspace.settings["docs"]["sharing"]["modes"]`; stapel-workspaces exposes
  no reader for workspace settings (its comm surface is `check_membership` /
  `check_capability` / `check_mandate`), and reaching into another module's
  rows is the seam violation the L2 canon exists to prevent. The intersection
  lives in `authz.effective_modes` and is the whole change when that surface
  lands.
- **No resolver ships.** A resolver answers "is this person in that
  container", which only the host knows. The registry and its validation are
  here; the resolvers are the product's.
- **Folder sharing is still not built** (axis §8): documents only in v1.

### Migrations

- `0004_sharing_access_and_links` — two additive tables, no data migration.

## [0.5.0] — 2026-09-02

### Added — the drive wave: starred, recents, search, usage, thumbnails

`tasks/stapel-drive-spec.md` §3.1-§3.4 and §3.6. The Google-Drive product
ships as a wave of this module rather than a second L2 module, so none of
this introduces a second owner of workspace document data — the new rows sit
next to the ones they annotate, behind the same `authorize()` choke point and
the same `DocsStorage` seam.

- **Starred** (`Star`) — per-user bookmarks on documents AND folders.
  `POST`/`DELETE /documents/<id>/star` and the folder twin,
  `GET /starred?workspace_id=`. Both verbs answer 204 whatever the previous
  state was: an idempotent verb that reports "already done" as a failure
  forces every client to read before writing. Gated by **`docs.view`**, not
  `docs.edit` — a star is a bookmark, and requiring edit would make "keep
  this handy" an act of authorship. Exactly one of the two target FKs is a
  `CheckConstraint`, so a meaningless row is refused by the database rather
  than by a convention some future call site forgets. `is_starred` now rides
  every folder and document envelope, annotated with `Exists` and **`null`
  for a principal with no user id** — the listings canon: "not applicable"
  is a third answer, and collapsing it into `false` tells an anonymous reader
  it un-starred something it never could have starred.
- **Recents** (`RecentEntry`) — `GET /recents?workspace_id=`, newest first,
  live only. The upsert lives in the **service layer**, on the three paths
  that actually hand a document to a person: `read_content`,
  `document_download_url` and an accepted `save_content`. Hooking the views
  instead would have meant the next read path added inherits nothing; hooking
  the service means a rejected save records nothing (it never happened) and a
  machine read — export, thumbnail rendering, revision replay — passes no
  user and leaves no trace. Capped by `RECENTS_MAX_PER_USER` (default 100),
  trimmed oldest-first on write. No events: a bus message per document open
  would be the noisiest topic in the fleet.
- **Search by name** — `GET /search?workspace_id=&q=[&limit=]`. Workspace
  scoped, tree-wide, case-insensitive `icontains` over live `Folder.name` and
  `Document.title`; each hit carries its `kind` and a root-first
  **breadcrumb** materialized from ONE folder-index query for the whole
  result set, because resolving each hit's ancestry on its own is the N+1
  that makes a search endpoint quadratic in tree depth the first time a real
  workspace gets deep (a test asserts the query count is constant across ten
  hits nested three deep). An absent or blank `q` is a 400 — a search
  endpoint that answers an empty query with the whole workspace is a listing
  endpoint wearing a search name, and the most expensive scan a client can
  trigger by accident. Deliberately **not** knowledge search: no FTS, no
  trigram in v1. The existing `?q=` filter on the documents listing stays for
  the in-folder case.
- **`docs.usage`** — the metering surface billing can consume:
  `{bytes_live, bytes_trash, bytes_total, documents, folders, by_type}` for
  one workspace. `bytes_total` is the SAME sum the 507 quota already refuses
  against (invariant I2, one sum), so a meter and a refusal can never tell an
  operator two different stories about how full a workspace is; trashed rows
  keep being charged in `bytes_trash`, because trash is not a discount.
  Composing an entitlement ceiling out of it is the host's glue — docs owns
  the measurement, never the price. **Authority is the same gate as
  `docs.create_document`**, one capability lower (`docs.view`): the surface
  is read-only, but the data is a workspace's, and "it only reads" is how a
  per-workspace corpus size and type mix becomes enumerable by id to every
  participant on the bus.
- **Image thumbnails** — `GET /documents/<id>/thumbnail?tier=` for
  `type=file` documents with an `image/*` mime. Server-side Pillow resize
  (new optional extra `stapel-docs[thumbnails]`, also folded into `[all]`),
  tiers a fixed `(160, 480)` ladder. Served through `authorize(docs.view)`
  and the storage seam and nothing else — never a second read path, and never
  a CDN, which is what would make a private workspace file's preview publicly
  addressable. Missing Pillow answers **503** the way a missing exporter
  dependency does, so a frontend falls back to a type icon instead of having
  to tell a silent empty answer from a broken deploy; a non-image, a non-file
  and a pending upload with no bytes answer 400, and so does a source Pillow
  refuses to decode — a caller's bad input is a 400, not a 500.

### Added — how a derived object stays inside invariant I2

The cached thumbnail is written under the document's OWN storage prefix
(`{PREFIX}/{workspace}/{document}/thumb.{head_seq}.{tier}.jpg`), and a
`Thumbnail` row registers the key. Both halves are load-bearing:

- `head_seq` in the key means a stale image is **unaddressable**, not merely
  unpreferred — a save bumps the seq, the next request asks for a key that
  does not exist yet, and the renderer runs. There is no cache to invalidate
  and therefore no invalidation to forget.
- the row exists because `services.purge_document` deletes **enumerated**
  keys, not a key prefix. An unregistered derived object would have outlived
  the document it depicts, in a module whose whole storage story is "content
  bytes live under this prefix and nowhere else, and purge destroys all of
  them". A test purges a document with two cached tiers and asserts both
  objects are gone from the bucket.

Thumbnail bytes are deliberately absent from the `document.storage_changed`
delta and from the quota sum: they were never charged, so removing them must
not credit anything back.

### Changed — GDPR and the account life cycle cover the new tables

- **Account erasure**: authorship keeps being *anonymized* (documents are
  co-produced workspace content and survive their authors), but `Star` and
  `RecentEntry` rows are *deleted* — they are one person's private view of
  the corpus, they mean nothing without that person, and an anonymized star
  is a bookmark nobody can reach and nobody can clear. The receipt gains
  `stars_deleted` / `recents_deleted`; document and workspace erasures gain
  `stars` / `recents`, and their `storage_objects` count now includes cached
  thumbnails.
- **`user.merged`** re-parents the new tables with **collision folding**,
  because both are unique per `(user, target)` and a blind `UPDATE` breaks
  the constraint exactly in the common case — a guest and their real account
  tend to look at the same things. A star folds to "still starred, once"; a
  recent folds to the **newer** `accessed_at`, since one person has one
  answer to "when did I last reach this" and it is the later one. A guest
  whose only trace is a bookmark still arms `MergeTargetNotReady`, so the
  event is redelivered rather than ACKed with the bookmark stranded.
  `check_lifecycle_pairs()` stays green.

### Changed — contract and configuration

- Two new settings: `RECENTS_MAX_PER_USER` (100) and `SEARCH_MAX_RESULTS`
  (50). Thumbnail tiers are **not** a setting: the tier is part of the URL a
  client caches against, and every extra rung is another rendered copy of
  every image in the bucket.
- Three new error keys (`error.400.docs_thumbnail_tier`,
  `error.400.docs_thumbnail_unsupported`,
  `error.503.docs_thumbnails_unavailable`) with ru/es catalogs.
- The HTTP surface grows 27 → 35 operations; the usage surface 54 → 67
  entries (`thumbnails.py` joins the surface roots). `docs/llms.txt` no
  longer fits the 6000-token ceiling, so the budget is raised to 7000 —
  deliberately, per the generator's own advice, rather than by shortening the
  intent lines that explain the module's gates.
- Migration `0003_drive_star_recent_thumbnail` — three new tables, additive.

## [0.4.1] — 2026-09-02

### Changed — a markdown document exports as markdown, not as its source

`?format=pdf` on an `md` document printed the body verbatim: literal `#`,
`**`, backticks and pipe-tables, laid out as one wrapped paragraph. The
exporter's own docstring admitted it ("md is rendered verbatim as plain text
in v1"), which is the giveaway — the format was in the supported list, the
endpoint returned 200, the PDF was well-formed and openable, and nothing
anywhere said the output was wrong. A caller could only find out by looking
at one.

- **The md path parses.** Python-Markdown produces HTML, the HTML is
  sanitized (below), and fpdf2's `write_html` renders it: headings sized off
  the 11pt body, `**bold**` and `*italic*` in the real bold and oblique
  faces, `-`/`1.` lists bulleted and numbered, fenced blocks in a monospace
  face, pipe tables as bordered grids, `[text](url)` as a clickable PDF
  annotation, `---` as a rule. Fpdf2's own defaults are overridden where they
  do not survive this font set: `<code>`/`<pre>` default to Courier, and
  headings to a dark red sized for a 12pt Times body.
- **Two fpdf2 defaults needed more than a style override.** A markdown table
  has no syntax for asking for borders, so the sanitizer emits the `border`
  attribute that gets fpdf2's full-grid layout — a table here looks like the
  csv exporter's, not like a rule under the header row. And fpdf2 draws a
  list marker with the PDF's *live* font rather than the one the paragraph
  is about to use, so every list under a heading got a bold 15pt `1.` beside
  11pt text; `HTML2FPDF_CLASS` (fpdf2's own hook) pins the marker to the body
  face, defensively enough that a future fpdf2 loses the fix instead of
  raising mid-export.
- **Three more DejaVu faces ship in `assets/`** — Oblique, BoldOblique and
  SansMono (v2.37; the two already there are v2.35). fpdf2 synthesizes
  neither italics nor a monospace, and its core fonts — Courier included, the
  one `<code>` would otherwise get — are latin-1 only, so a cyrillic
  identifier inside a fenced block had no way to render. The three faces are
  registered on the md path alone: `add_font` re-parses the TTF per document,
  and a txt or csv export has no use for them. ~1.6MB in the wheel, in
  exchange for a renderer that needs nothing installed on the host.
- **No WeasyPrint.** The rendering verdict in the design corpus said
  WeasyPrint; it drags system pango/cairo, and the fleet default is
  zero-infra. The deviation is deliberate and recorded by the track lead:
  fpdf2 + Python-Markdown keeps `[pdf]` pure-python and `pip install`-able
  everywhere the rest of the module is.

### Security — a document body is user input reaching an HTML renderer

Markdown passes raw HTML through untouched, so switching the md path to an
HTML renderer handed whoever typed the body a renderer to aim. `<img>` is the
sharp edge: fpdf2 resolves an image `src` **by fetching it**, which turns
"export this document" into "make this server issue a request of the author's
choosing" — a note containing `<img src="http://169.254.169.254/...">` is a
metadata-service probe with an export button on it. fpdf2 2.8.8's own
resource-access policy blocks private addresses, but that is a second line,
not the design.

- **An allowlist sanitizer sits between the parser and the renderer**
  (`_sanitize_html`). Images are dropped entirely before fpdf2 sees one, alt
  text kept as italics so a figure's caption is not lost; `<script>` and
  `<style>` lose their content, not just their tags; only `http`, `https` and
  `mailto` hrefs become annotations (a `javascript:` anchor keeps its text and
  loses its link); every attribute outside a per-tag allowlist is stripped,
  including a `<font face=…>` naming a font the document never registered,
  which is a 500 written by the body's author; unknown tags keep their text
  and lose their markup; and unbalanced tags are closed here rather than
  crashing fpdf2's parser mid-document.

### Added — `markdown` in the `[pdf]` and `all` extras

`markdown>=3.4`. **Missing, it raises `ExporterUnavailable` → 503**, the same
answer as a missing fpdf2, and explicitly *not* a fallback to the old
verbatim rendering: the fallback would return 200 with a PDF nobody can tell
from a rendered one, and the incomplete install would never be discovered.
txt and csv are untouched by the absence — only the md path imports it, and
a test pins that.

`tests/test_export.py` reads the **text layer** of the PDFs it renders
(pypdf, added to the CI test deps) and asserts the markdown markers are gone
while the content is not, that the bold/oblique/mono faces are actually
embedded, that the link became an annotation, that cyrillic survives inside a
code block, and that a body with a remote image exports without fetching it.
The parse-and-sanitize claims are also asserted directly on the pure
function, so they hold in a checkout without pypdf.

**Patch, not minor**: no public name, setting, error key, endpoint or schema
changed — the same request returns the same media type, rendered properly.
The new dependency is optional and inside an existing extra.

## [0.4.0] — 2026-08-30

### Added — `user.merged`: a guest's documents survive being folded into an account

This module knew one thing about an account's end: erase it. When a visitor
who wrote as a guest signs in with an authenticator an existing account
already holds, stapel-auth folds the two and emits `user.merged` — the
opposite instruction. Nothing here answered it, so the guest's documents kept
an `owner` that could no longer sign in: never listed for the survivor, and
never erased either, because no erasure is ever requested for an account that
was *merged* rather than closed. The failure has no symptom at the seam —
nothing raises, nothing retries, nothing is logged — and the first report of
it is a person saying the notes they wrote are gone.

- **`user.merged` is subscribed in `stapel_docs.actions`** and re-parents
  every column this module keys by a user, in one transaction:
  `Document.owner`, `Folder.created_by`, `Revision.created_by`,
  `DocumentUpdate.author_id` (the CRDT journal's attributed writes) and
  `UploadSession.created_by`, so an in-flight upload can still be finalized
  by the account that now holds the ticket.
- **Re-parent, not anonymize.** The erasure path nulls exactly these columns
  because "nobody wrote this any more"; a merge sets them because somebody
  else did. The two events reach the same tables through the same registry,
  and answering only one of them is a silent wrong answer to the other.
- **An ordering lag is retried; a bad id is not.** A guest who authored
  nothing here is a quiet no-op (also the at-least-once idempotency path); a
  guest who authored rows while the survivor has no user row here *yet*
  raises `MergeTargetNotReady`, so the outbox redelivers instead of marking
  the event delivered and stranding the documents. A malformed or missing id
  is logged and ACKed — `ValidationError` included, which is what Django
  raises for an uncoercible UUID and is not a `ValueError`, the guard a
  poison payload otherwise escapes through and loops on forever.
- `schemas/consumes/user.merged.json` and the MODULE.md / readme action
  tables carry the contract. `tests/test_user_merged.py` pins the rows
  moving, a redelivery moving nothing further, every malformed shape ACKing,
  an event about users with no rows here doing nothing — and
  `stapel_core.lifecycle.E001` returning `[]`, so the pair cannot be broken
  again without a red test.

**Minor, not patch**: a new consumed action is public surface. Requires no
new stapel-core API; the E001 check that names the gap ships in stapel-core
0.52.1.

## [0.3.0] — 2026-08-23

### Added — subject-scoped erasure: docs answers stapel-gdpr for `account`, `workspace` and `document`

stapel-gdpr 0.5.0 generalized account closure into an erasure keyed by a
subject (`{subject_type, subject_key}`) and made owner silence visible: a
declared owner that never answers is named at boot (`gdpr.W006`) instead of
discovered when a request times out. This module was reachable only through
`user.deleted`, i.e. only for accounts — a deleted workspace or a deleted
document left its rows, its journal, its revisions and every object of its
history in place, with nothing that could prove otherwise.

- New `erasure.py`: `erase(subject_type, subject_key, workspace_id=None)`
  returns what it removed, per subject:
  - `document` — the row, the update journal, every `Revision` and every
    storage object of the document's history, through the module's own
    `services.purge_document` (O(document), idempotent, objects deleted
    after commit). Live or trashed alike: an erasure is not a trash
    operation and does not wait out `TRASH_RETENTION_DAYS`. Upload sessions
    still pointing at the document die with their staging objects.
  - `workspace` — every document of the workspace as above, then the folder
    tree and every pending upload session with its staging object.
  - `account` — unchanged policy, now reached through the same entry point:
    authorship is **anonymized, not deleted** (storage-verdict §3), because
    documents are co-produced workspace content and destroying them would
    erase other members' data under the banner of erasing one person's.
- New consumers in `actions.py` (one module, both handlers — that
  co-location is what makes an `alive` answer evidence the erasure path is
  *consumed*): `gdpr.erasure.requested` → the erase above plus a
  `gdpr.section.erased` receipt `{owner: "docs", subject_type, subject_key,
  receipt_id, counts}` emitted in the same transaction; `gdpr.owner.probe` →
  `gdpr.owner.alive {owner, subject_types}`.
- Contracts committed: `schemas/emits/gdpr.section.erased.json`,
  `schemas/emits/gdpr.owner.alive.json` (validated on every emit under
  `VALIDATE_SCHEMAS`) and `schemas/consumes/{gdpr.erasure.requested,
  gdpr.owner.probe,user.deleted}.json` — the consumed half of the comm
  surface was undeclared until now.
- `DocsGDPRProvider.anonymize()` / `.delete()` now return the per-model
  counts they changed (they returned `None`); the base `GDPRProvider`
  ignores a return value, so the orchestrator's in-process path is
  unaffected, and the counts are what an `account` receipt carries.

Refusals are deliberate: a subject type this owner does not claim is
ignored (gdpr opens no part for it), and a request whose `workspace_id`
contradicts the document's row raises instead of receipting zeros — the
part then times out visibly rather than certifying an erasure that never
happened.

Host wiring (one line, no new setting here):

```python
STAPEL_GDPR = {"DATA_OWNERS": {"docs": ["account", "workspace", "document"]}}
```

**Minor, not patch**: new public module (`stapel_docs.erasure`), two new
emitted actions and two new consumed ones — public surface grew.

### Deprecated — `user.deleted`

Still consumed, and now routed through `erase("account", …)` instead of
calling the provider beside it. stapel-gdpr emits it alongside
`gdpr.erasure.requested` for one minor and removes it in its 0.6.0; this
module's handler goes with it. It deliberately emits **no** receipt: the
account erasure that carries a correlation_id arrives as
`gdpr.erasure.requested` and is receipted there.

### Not in this release

Share and mandate grants: v1 has none to erase — every verdict comes from
`workspaces.check_capability` and the sharing axis's own grant rows
(whitelist/link) do not exist yet (`SHARING`, phase 3). They join the
`document`/`workspace` erasures in `erasure.py` when they land.

## [0.2.4] — 2026-08-21

### Changed — `stapel-core` floor raised to 0.27.0

CI installs `stapel-core` off git HEAD (currently 0.32.0), which has grown
`error.503.mandate_unavailable` (+ ru/es catalogs) since this module's floor
was last raised. `docs/errors.json` regenerated against that HEAD now carries
the entry, and the drift gate (`tests/test_contract.py`) went red against the
stale committed artifact until it did. Floor raised to >=0.27.0 (the release
that registers the error) so the committed contract and the declared minimum
agree again; `make contract` re-run to pick it up.

## [0.2.3] — 2026-08-21

### Fixed — `_save_snapshot` mutation and outbox emits shared no atomic block (EMIT003)

`_save_snapshot` (the single snapshot-save path behind `save_content` and
`create_document`) wrote the document/revision rows and then called
`events.emit_document_updated` / `events.emit_storage_changed` relying on the
*caller's* `transaction.atomic()` — invisible to
`stapel_core.lint.emit_check`'s lexical EMIT003 gate and, more importantly,
inconsistent with every other emit site in this module, which all open their
own `transaction.atomic()` around mutation + emit. Wrapped the writes and
both emits in the helper's own `transaction.atomic()`; nesting inside the
callers' wider atomic block is a safe savepoint
(`stapel_core.comm.mutate_and_emit`'s documented nesting guarantee) and now
the helper is self-sufficient under the static gate.

## [0.2.2] — 2026-08-15

### Changed — `stapel-core` floor raised to 0.26.0

`docs/errors.json` carries an `owner` per entry, and only stapel-core 0.26.0
emits it. The floor lagged behind, so a consumer resolving an older core
regenerated an artifact without `owner` and the drift gate went red — the
field was declared but never required. The floor now matches the artifact
that is committed.

## [0.2.1] — 2026-08-15

### Added — the error catalogs this module owns (ru, es)

This module registers 32 `error.*.docs_*` keys and shipped a catalog for
none of them. Since stapel-core 0.23.1 a consumer resolves a key it does
not own from the **owner's** catalog, and since 0.22.0 a writer may only
translate keys it owns — so shipping nothing did not leave the gap open for
someone else to fill legally: it made every consumer render the English
literal, and the one that filled it locally was maintaining a shadow of
this module's canon that nothing here would ever update.

`translations/errors.ru.json` + `translations/errors.es.json` (32 keys
each) now ship, with the `translations/.state.json` provenance sidecar,
and both are in the wheel (`package-data`) — a catalog that reaches only
the repository is a catalog no deployment can read. Languages match what
every other stapel library with error keys promises: en canon in
`errors.py`, ru and es as catalogs.

Provenance is recorded, not implied: the curated stapel-translate builtin
corpus carries none of these keys, so every value is a machine translation
(`origin: llm`, the gate's unreviewed counter) reproduced offline from the
table in `tests/test_error_i18n.py`. `tests/test_error_i18n.py` is also the
gate — coverage scoped to ownership, no foreign keys, placeholders
preserved, byte-stable, no drift.

Strings and packaging only: no code, no schema, no migration changed.

## [0.2.0] — 2026-08-14

### Added

- **Resource invariants** (security audit DOCS-01): hard ceilings on every
  accepted byte — snapshot body, journal update and batch, upload blob and
  exporter input — plus an opt-in per-workspace stored-byte quota, all in
  the `STAPEL_DOCS` namespace (`MAX_BODY_BYTES`, `MAX_UPDATE_BYTES`,
  `MAX_UPDATES_PER_REQUEST`, `MAX_UPLOAD_BYTES`, `MAX_EXPORT_BYTES`,
  `WORKSPACE_QUOTA_BYTES`).
- **Upload sessions are capabilities, not slots**: a ticket carries an
  expiry (`UPLOAD_SESSION_TTL_SECONDS`), belongs to the user who opened it
  (anyone else needs workspace `manage`), accepts an optional declared
  sha256, and is consumed by a conditional state transition. Finalize
  validates the STORED object — existence, ceiling, declared size and
  checksum — before promoting it. Open sessions per workspace are capped
  (`MAX_PENDING_UPLOADS_PER_WORKSPACE`) and uploads can be restricted to a
  MIME allowlist (`UPLOAD_ALLOWED_MIME_TYPES`).

- **The internal create path is scoped to a caller** (audit DOCS-02):
  `docs.create_document` now carries its authority in the payload —
  `actor_id` is authorized through the same choke point as an HTTP caller
  (`docs.edit` in the target workspace), or the calling service must be
  listed in `INTERNAL_TRUSTED_SERVICES`; an `owner_id` other than the actor
  must itself be a workspace member. `INTERNAL_REQUIRE_CALLER` (default on)
  makes an unbound call a refusal, and a workspaces outage is a 503, never
  an allow.
- **Scheduled retention**: `stapel_docs.tasks` with `purge_expired_trash`
  (plain callable; a celery shared task when celery is installed) and
  `get_docs_beat_schedule()` on the configured `TRASH_PURGE_SCHEDULE`, plus
  system check `stapel_docs.W030` for a host whose beat schedule never runs
  the purge.

### Changed

- **Requires stapel-core >= 0.24.0** (was `>=0.15.11`). Not driven by a new
  call: this module declares `import_strings=("STORAGE",)`, and on an older
  core an import-string key was still read from the environment unless the
  module listed it in `no_env` — which this one never did, so a bare
  `STORAGE` environment variable could choose the storage class the package
  loads. The download-URL refusal below is a property of that class
  (`mints_expiring_urls = True`), so on an older core "may this deployment
  hand out permanent public links" is a question the environment can answer.
  Core 0.24.0 makes every `import_strings` key implicitly `no_env`, with
  `env_overridable=` as the per-key opt-out.
- **UPGRADE NOTE — an ownerless upload ticket needs `manage`** (audit
  DOCS-03). The owner binding on upload finalize was skipped entirely when
  `created_by_id` was falsy, which is exactly what GDPR anonymize leaves
  behind, so any workspace editor could spend such a ticket. A ticket with
  no owner now takes the same `docs.manage` escalation as somebody else's
  ticket (403 `error.403.docs_upload_owner` otherwise) — including for the
  user who originally opened it, since the row no longer says they did.
  Hosts that anonymize users mid-upload should expect those pending
  tickets to be finalized by a manager or abandoned to expiry.
- **An upload whose object cannot be measured is refused** (audit
  DOCS-03), with the new 400 `error.400.docs_upload_unmeasurable`. Finalize
  used to fall back to the size the CLIENT declared when the store could
  not report one — and `DjangoStorageBackend.head_object` turned any
  exception from `storage.size()` into "exists, size unknown", so a storage
  fault became an upload that passed the ceiling unchecked and charged the
  workspace quota nothing (zero, for a ticket opened without a declared
  size). `head_object` no longer swallows those errors: a backend that
  cannot size an object raises, and the finalize fails.
- **UPGRADE NOTE — the workspace storage quota ships switched on** (audit
  DOCS-03). `WORKSPACE_QUOTA_BYTES` defaults to **10 GiB** instead of `0`.
  It was the one limit in the hard-invariants block that shipped off, so
  the only thing bounding a workspace's growth was the object store's
  invoice. A workspace already holding more than 10 GiB of heads +
  revisions will start refusing writes with 507
  `error.507.docs_workspace_quota` until the setting is raised. **To keep
  unlimited storage** set `STAPEL_DOCS["WORKSPACE_QUOTA_BYTES"] = 0` — `0`
  remains "no quota", now as a deliberate opt-out rather than as the
  shipped default.
- **UPGRADE NOTE — uploads ship with a real MIME allowlist** (audit
  DOCS-03). `UPLOAD_ALLOWED_MIME_TYPES` no longer defaults to `[]`, and
  `[]` no longer means "anything": an empty allowlist now allows nothing,
  and the shipped default is `stapel_docs.conf.DEFAULT_UPLOAD_MIME_TYPES`
  (text/data, PDF/RTF, Office + OpenDocument, enumerated image types, and
  common audio/video). Deliberately absent: `text/html`,
  `image/svg+xml`, `application/javascript` and executables — active
  content a host serving `MEDIA_URL` inline would run in its own origin.
  An upload that declares **no** `mime_type` is unknown content and is now
  refused too (400 `error.400.docs_upload_mime`), so clients that omitted
  the field must start sending it. **To restore the old
  accept-anything behaviour** set
  `STAPEL_DOCS["UPLOAD_ALLOWED_MIME_TYPES"] = ["*/*"]` — the open position
  is now something a deployment states, not something it inherits from an
  unfilled setting. Widening the list for your own types is the normal
  path.
- **UPGRADE NOTE — download URLs must be able to expire** (audit DOCS-03).
  `GET /documents/{id}/download` and `GET /documents/{id}/revisions/{id}/download`
  now refuse with **503 `error.503.docs_download_url_unavailable`** when the
  configured storage backend cannot honour `DOWNLOAD_URL_EXPIRES_SECONDS`.
  The default `DjangoStorageBackend` cannot: it could only return
  `storage.url(key)` — a permanent, public `MEDIA_URL` link that outlives
  the membership it was minted for and never passes `authorize()` again,
  which contradicted the module's own "there is no second read path".
  Deployments on the default backend lose those two URL endpoints unless
  they act; the authorized `GET …/content` stream serves the same bytes and
  is unchanged, as are uploads, and `S3Backend` (which really signs) is
  unaffected. **To restore the old behaviour** set
  `STAPEL_DOCS["ALLOW_UNEXPIRING_DOWNLOAD_URLS"] = True` — that is now an
  explicit, checked decision to hand out permanent public links (system
  check `stapel_docs.W032`), while `stapel_docs.W031` warns any deployment
  whose download path is refusing. A `DocsStorage` implementation advertises
  its ability by setting `mints_expiring_urls = True`; the fail-closed
  default is `False`, so a host backend that signs must say so.
  `DjangoStorageBackend.presigned_get_url` also no longer swallows storage
  errors into a bare object key returned as if it were a URL.
- **Object-store writes follow the database outcome** (audit DOCS-02):
  snapshot writes are compensated when the surrounding transaction fails
  and object deletes are deferred to `on_commit`, so a rolled-back save
  leaves no orphan and a rolled-back purge cannot destroy bytes a surviving
  row still points at. New seam: `services.storage_transaction()`.
- **`file` bodies have exactly one door.** `DocTypeSpec` gained
  `body_mutable`; the content PUT (and create-with-body) now refuses types
  that own their own write path — `type=file` (upload flow) and types whose
  spec vanished from the registry, which the storage verdict already
  promised were read-only. Revision restore replays already-accepted bytes
  and is unaffected.
- The upload ticket response carries `expires_at`.

## [0.1.1] — 2026-08-10

### Fixed

- **The stapel-core pin was a scaffold placeholder** (`>=0.3.0,<0.4`) that
  never got set: next to any real host (fleet services floor core at
  `>=0.18`) pip either refused to resolve or silently downgraded
  stapel-core to 0.3.2 — observed live while mounting docs into
  iron-recordings. Now `>=0.15.11,<1.0`, a measured floor: the full suite
  is green against 0.15.11, 0.16.0 and 0.18.0 (probed 2026-08-10); older
  cores are unmeasured and unclaimed.

## [0.1.0] — 2026-08-10

First release. Document/folder tree with versioning, exports, and the comm-bus
surface other modules need to attach documents to their own entities.

- **Versioning substrate** — document/folder models, `DOC_TYPES` registry,
  storage seam, authz choke point.
- **HTTP surface** — tree, content with optimistic locking, journal,
  revisions, trash, uploads.
- **Comm surface** — `docs.create_document` action, emitted-actions join
  point for other modules, GDPR anonymize provider, INGEST seam.
- **PDF exporter** — txt/md/csv via fpdf2 with bundled DejaVu unicode fonts.
- Full lifecycle proven end-to-end against a real host project (auth +
  workspaces + docs); contract quintet (`docs/{schema,flows,errors,capabilities,llms}`)
  drift-gated in CI.
