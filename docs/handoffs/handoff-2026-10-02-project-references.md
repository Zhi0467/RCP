# Project references in chat

Date: 2026-10-02
Status: design drafted 2026-10-02 after two answered questions. Implementation
has not started. Nothing below is built yet.

## Problem

A chat cannot see RCP's own records. A Discuss turn reads the state repository
(graph, `research.md`, the paper introduction) and the registered repositories.
It cannot read stored artifacts or episode reports, which live in
`<data>/artifacts`. The human cannot hand one over either. Uploads accept only
files from disk, and RCP never shows a path to copy.

The one route that stages an artifact into a turn is the artifact comment
(`ArtifactContextRequest`). It is bound to the artifact's origin chat and
session, so it cannot bring a report into any other chat.

Seen live: a project chat asked for "the last auto-research episode report".
It found an older report only because a copy had been kept in the repository.
The newest report existed only in RCP storage, and the agent asked the human for
a path they had no way to give.

## Settled with the human

- **You attach, the agent reads.** The human puts a reference into the composer.
  RCP copies that item into the turn. Agents get no new lookup command and no
  read access to RCP storage. Everything outside a turn's references stays as
  out of reach as it is today.
- **First scope:** artifacts (episode reports are artifacts), graph nodes, and
  the paper introduction. Runs, episode timelines, and terminal output are out
  of scope.

## Design

### One reference shape

A reference is `{kind, id}`, with these kinds:

- `artifact`: `artifact_id`. This includes episode reports, which are captured as
  artifacts with `supplier="episode_ending"`.
- `node`: the graph node id.
- `paper`: no id. It means the project's paper introduction.

`RunRequest` gains `references: list[ProjectReference]`, capped in `limits.py`.
The cap is the same 8 used for attachments. It applies to node and project
chats, in both Discuss and Work.

### Admission resolves, staging copies

The flow matches attachments, so recovery and remote hosts behave the same way.

1. **Admission** (inside chat task start, next to the attachment claim) checks
   that each reference exists in this project. It then freezes the current state:
   the artifact's `current_version`, or the graph revision for a node or the
   paper. A missing item rejects the turn with a 422. RCP never drops a
   reference and sends the text alone.
2. **Staging** copies the frozen bytes into
   `<stage>/inputs/project-references-v1-<operation_id>/`, read-only, the same
   way `ChatAttachmentStore.stage` does. A remote stage uses `put_directory`.
   Task recovery re-stages the same frozen versions.
   - An artifact copies its stored bytes for that version, under the existing
     `CHAT_ARTIFACT_MAX_FILE_BYTES` cap.
   - A node becomes one small JSON file. It holds the node record and its
     one-hop relations at the frozen revision, in the shape `ChatContext`
     already builds for a focused node.
   - The paper copies `introduction.md` at the frozen revision.
   Every kind is copied, even when the agent could already read the source
   file. That keeps one code path, works when the state repository is not on the
   execution host, and pins what the human pointed at.
3. **Prompt.** The copied folder joins the turn's read dirs, as attachments do.
   `_attachment_items` gains a "Project references" line per item: kind, display
   name, version or revision, and path. Every reference renders through the same
   function, so the prompt describes exactly the files that were staged.

A reference is context, not authority (invariant 5). The copies are read-only.
A referenced artifact is not editable through this route. Editing stays with
artifact comments and their origin session.

### Transcript

The human turn stores display metadata for each reference: kind, id, name, and
frozen version. Bytes and paths are not stored, the same as attachments. Each
reference shows as a chip that opens the item: the artifact viewer, the node in
Research, or the paper.

### One link format for copy, paste, and drop

Notification links already use one generic form,
`#/projects/{p}/targets/{t}/{kind}/{id}`, which App resolves into its own route
(`web/src/notificationLinks.ts`). This design adds the kinds `artifact`, `node`,
and `paper` to that pattern. The link is both:

- a deep link that opens the item when clicked or pasted into the address bar;
- the token the composer turns into a reference chip.

Pasting plain text that is a link to the same project becomes a chip. A link to
another project stays as text. A drag carries the same link as `text/uri-list`.

### Composer gestures

- **Drag:** rows in Artifacts (files and reports) and the report link on a Runs
  episode card can be dragged. The composer accepts them next to its existing
  file drop.
- **Copy reference:** a button in the artifact viewer's app chrome (not inside
  the sandboxed frame), in a node's detail panel, and on the paper. It writes the
  link to the clipboard.
- **Paste:** the composer recognizes the link, as above.
- **Pick:** `+` becomes a small menu, "Upload file" and "From project…". The
  picker searches artifacts, reports, nodes, and the paper with the list
  endpoints that already exist.

Chips sit beside attachment chips and follow the same rules. They can be
removed. They are kept as a per-chat draft. They count against the reference
cap. A turn with references cannot be sent as a live steer, which matches
attachments, so it queues as the next turn.

### Not doing

- No lookup verb for agents and no read root on `<data>/artifacts`. The human
  chose explicit attachment.
- No live mount. A reference is a snapshot taken at send time.
- No `@` typing autocomplete for now. The picker covers it. Add it later if
  wanted.
- No references to runs, timelines, or terminals.

## Contract changes

- Shared: `RunRequest.references` in `src/rcp/service.py`, and `web/src/types.ts`.
- Spec: replace the "no message references" sentence in
  `docs/specs/conversations-episodes-and-watchers.md` (human input). The new text
  should say what references are, and that annotations still carry none. Add
  the link kinds to `docs/specs/api-web-and-desktop-projections.md`.

## Slices

1. Backend: model, limit, admission freeze, staging for the three kinds,
   recovery re-stage, prompt line, transcript metadata. Tests cover admission
   (missing item gives a 422; the frozen version is used), staging (read-only
   copy; remote `put_directory`), recovery re-stage, and the prompt rendering the
   staged object.
2. Web: link kinds and App resolution, composer chips, paste and drop
   recognition, the `+` menu and picker, Copy reference buttons, draggable
   Artifacts rows. Node tests cover link parsing and chip draft state. Run
   `npm --prefix web run build`.
3. Served-app journey on disposable data with a seeded episode report. Drag the
   report into a different chat, send in Discuss, and check that the staged copy
   and prompt line are there and the chip opens the viewer. Repeat with paste and
   with the picker, for a node and for the paper.

## Close when

Slice 3 holds on a served app, and the specs above describe references.
