# Project references in chat

Date: 2026-10-02
Status: design settled with the human on 2026-10-02. Revised the same day after
an xhigh astra design review. Implementation has not started. It ships in the
same PR as this design.

## Problem

A chat cannot see RCP's own records. A Discuss turn reads the state repository
(graph, `research.md`, the paper introduction) and the registered repositories.
It cannot read stored artifacts or episode reports, which live in
`<data>/artifacts`. The human cannot hand one over either. Uploads accept only
files from disk, and RCP never shows a path to copy.

The one route that stages an artifact into a turn is the artifact comment
(`ArtifactContextRequest`). It is bound to the artifact's origin chat and
session, and it asks for edits, so it cannot bring a report into any other chat.

Seen live: a project chat asked for "the last auto-research episode report".
It found an older report only because a copy had been kept in the repository.
The newest report existed only in RCP storage, and the agent asked the human for
a path they had no way to give.

## Settled with the human

- **You attach, the agent reads.** The human puts a reference into the composer.
  RCP copies that item into the turn. Agents get no lookup command and no read
  or write root on RCP storage.
- **First scope:** artifacts (episode reports are artifacts), graph nodes, and
  the saved paper introduction. Runs, timelines, and terminals are out of scope.
- **One PR** for design and implementation.

## Design

### Selector

The client sends selectors. The server resolves them.

- `{kind: "artifact", artifact_id}`. This covers episode reports, which are
  artifacts with `supplier="episode_ending"`. An artifact keeps its provenance
  but never adopts its origin episode or session.
- `{kind: "node", node_id, branch_id}`. `branch_id` is null for main. The node is
  read from that source target, which can differ from the chat's own target. The
  chat's graph target and authority never change (invariant 5). A missing branch
  or node fails closed.
- `{kind: "paper"}`. The saved, canonical `introduction.md`. Editor drafts are
  excluded. The paper is project-wide, including in branch chats.

`RunRequest.references` holds the selectors. Duplicates are dropped on the
client and refused on the server.

### Limits

References share the attachment caps in `limits.py`. Attachments plus references
total at most 8 items and 32 MiB per turn, with each item at most 16 MiB. A node
snapshot is capped at 256 KiB.

### Admission retains bytes

Artifact versions are pruned and artifacts expire. The paper is saved without
a graph Patch. So admission copies the bytes. Recording a version number alone
is not enough.

At accepted chat task admission (`api/tasks.py`, beside the attachment claim):

1. A reference resolver reads each source through its owner:
   - artifact: `read_artifact_bytes` at `current_version`, under the artifact
     lock;
   - paper: `PaperService` through `StateWorkspace`, after a confirmed refresh
     for a remote state repository, never a cached read presented as current;
   - node: one coherent snapshot of the source target. It holds the node record
     and its one-hop relations, in the shape `ContextAssembler` builds for a
     focused node, plus the full `GraphHeadRef`.
2. `ChatAttachmentStore` gains a server-created snapshot batch. It stores those
   bytes with hashes and the same integrity checks, retention, claim, and release
   as uploads. A failed admission releases it.
3. The request carries only server-made descriptors: kind, display name, source
   identity, version or graph head, content hash, size, and media type.

Staging, remote transfer, and recovery read only the retained batch. They never
resolve a source again. A source changed or deleted after admission does not
change the turn.

### Staging and prompt

The existing attachment staging copies the batch read-only into the turn's
`inputs/`. The four launch owners already stage attachments:
`runs/tasks/discuss.py`, `work.py`, `auto_research_child_work.py`, and
`experiment_loop.py`. Each consumes the reference pointers there. Mechanical
staging is shared. Authority policy stays with each owner.

The prompt lists references in their own read-only block. It names kind, display
name, version or graph head, and path. It never reuses the artifact-comment
item, which asks for in-place edits. The block renders from the same staged
pointers used for read dirs.

Work launches with references carry the explicit write deny on artifact storage,
the same deny that artifact context carries today. File modes are not provider
enforcement. The existing Work boundary stays as it is.

Invariants held: 4 and 4b (no new capability, no graph authority from
references), 5 (context is not a write root), 6 (no canonical writes), 9 (failed
scratch kept), 10 and 10c (stable chat scratch rules unchanged), 10d (no
transcript rehydration), 10g (episode, session, stage, and Stop fences
unchanged).

### Routes

Accepted on `/tasks/node_chat` and `/tasks/project_chat`, in Discuss and Work, on
main and branch targets. That includes ordinary chats about Experiment nodes.
Resume and Retry reuse the admitted batch.

Refused explicitly, never dropped silently:

- `/steer`
- Experiment `/run`
- episode start, continue, and mail
- seed and refresh
- paper coach
- merge
- artifact-edit requests

Question and watcher follow-ups clear references wherever they clear
attachments. WebMCP Send stays reference-free, both in its schema and in
execution.

There is no queueing. While a turn runs, a draft with references stays a draft,
the same as attachments. It is sent when the human submits the next turn.

### Transcript

The human turn stores display descriptors: kind, name, source identity, frozen
version or graph head. Bytes and paths are not stored, the same as attachments.
A chip opens the current item. Its label shows the frozen version, so the human
can tell when the item has changed since.

### Links

The generic link `#/projects/{p}/targets/{t}/{kind}/{id}` in
`web/src/notificationLinks.ts` gains the kinds `artifact`, `node`, and `paper`.
The paper's id is `introduction`. Every segment is encoded. The `targets`
segment carries the node's source branch. Node links resolve through
`graphViewHash`, which keeps `branch_id`. A copied link is a full, address-bar-ready URL.
The composer recognizes the hash part.

Paste turns a link to the same project into a chip. Unrelated pasted text is
kept. A link to another project stays text. A drag carries the link as both
`text/uri-list` and `text/plain`.

### Composer gestures

- **Copy reference**, in trusted app chrome, never inside the sandboxed viewer
  frame: the artifact viewer, the node detail panel, and the paper. A clipboard
  failure shows a notice.
- **Paste**, as above.
- **Drag** from Artifacts rows and the Runs report link. Tauri's native drop
  handler can intercept drops in WKWebView, so this needs a live check. If
  internal drags cannot reach the page there, the desktop app does not offer
  dragging, and Copy and the picker cover it.
- **Pick:** `+` becomes "Upload file" and "From RCP…". The picker browses like
  Finder: folders for Reports, Artifacts, and Nodes, plus the Paper, and a search
  across all of them. It reads
  the committed target snapshot for nodes, `/paper` for the paper, and the
  saved artifact inventory (`/artifacts`, the Artifacts tab) for artifacts and
  reports. Temporary turn outputs are not listed; they can still be referenced
  by dragging or copying from their run card.

Drafts keep chips per project, target, and chat, beside attachments.

### Not doing

- No agent lookup verb, and no read root on `<data>/artifacts`.
- No live mount. A reference is a copy taken at admission.
- No `@` autocomplete for now.
- No references to runs, timelines, or terminals.

## Contract changes

- Shared: `RunRequest.references` and the reference descriptor in
  `src/rcp/service.py`, mirrored in `web/src/types.ts`.
- Specs: attachments and human input in
  `docs/specs/conversations-episodes-and-watchers.md`, which replaces the "no
  message references" sentence and keeps annotations reference-free; link kinds
  in `docs/specs/api-web-and-desktop-projections.md`;
  reference reads in `docs/specs/paper-artifacts-and-result-views.md`.

## Slices

1. Backend:
   - selectors, the resolver, and retained snapshot batches in the attachment
     store;
   - admission and rollback, and the route guards;
   - staging in the four launch owners, with the Work write deny;
   - the prompt block and transcript descriptors.

   Tests: the frozen bytes survive a source change or deletion, recovery
   re-stages the same batch, refused routes return 422, a node keeps its source
   branch, and the prompt renders from the staged pointers.

2. Web:
   - link kinds and resolution, chips, and drafts;
   - paste and drop recognition;
   - the `+` menu and picker;
   - Copy reference buttons and draggable rows.

   Node tests and `npm --prefix web run build`.

3. Journeys on disposable data with a seeded episode report:
   - Discuss and Work, with local and remote stages, and main and branch
     sources;
   - a source edited and deleted after send;
   - a failed transfer;
   - drag, paste, and picker in a browser and in WKWebView.

## Close when

Slice 3 holds, and the specs above describe references. Then delete this
handoff in the same change.
