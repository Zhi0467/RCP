# Live artifacts and the docked viewer

Date: 2026-09-29
Status: design settled with the human on 2026-09-29. Implementation has not
started. Docs and code land in one PR.

Close this handoff when all of these hold on a served app with disposable data:

- a chat turn launches a helper job and writes a live loss curve; the page
  redraws while the job runs, stops refreshing when it ends, and still renders
  from its saved final data after the job's files are deleted;
- a comment sent from the viewer on a chat artifact comes back as the next
  version in the same viewer, in the artifact's own chat session, and Undo
  returns to the previous version;
- the same holds for an Experiment turn's artifact and for an episode report,
  and the next Experiment turn in that session re-opens its own master;
- the viewer docks, floats, resizes, and goes full screen inside the RCP
  window, and the desktop app opens no native preview window;
- an update from `main` moves every stored report out of SQLite, and a team
  server update rehearsal passes on a copy of real data.

## Why

Today three things make artifacts hard to work with:

- The desktop opens every preview in a separate native window that floats over
  the app.
- Commenting is a round trip. Selections are staged into the chat composer, the
  human goes back to the chat to send, and an edit comes back as a candidate to
  Accept or Reject.
- Only chat artifacts can be commented on. Experiment and Auto-research turn
  artifacts cannot, and neither can a report whose episode did not end in a chat
  turn. Reports can never change.

The human wants every artifact editable in place, commented on from where it is
viewed, and able to show data that keeps changing after the agent's turn ends.

## Settled

### One artifact model

- One `Artifact` class replaces the three current records: task artifact
  descriptors, episode report records, and legacy result views.
- It has two suppliers. A **turn** (chat, Experiment, or Auto-research worker)
  makes artifacts in its artifact directory. An **episode ending** makes one
  report. The supplier's own code sets the artifact's rules when it creates
  the record: a turn artifact expires unless kept; a report never expires.
- The record stores its supplier as data. Viewing, editing, versions, and live
  data never branch on it.
- Result views were already retired as a creatable kind. Their remaining
  legacy rows, routes, prompt section, and `result_view` request plumbing are
  deleted. Legacy rows migrate into `Artifact` records.

### Storage

- Artifact bytes are files under the RCP data directory, one folder per
  artifact. SQLite holds only metadata.
- A turn's artifacts are copied into that folder when RCP discovers them, within
  the existing per-turn count and size caps. Viewing no longer proxies from a
  local or remote stage.
- Each artifact keeps its original plus its last N versions, capped by total
  bytes. N and the byte cap live in `limits.py`.
- **Keep** now only stops expiry. RCP no longer writes an `artifacts/` folder
  into the state repository. Files already kept there stay where they are; RCP
  imports them into its own storage and stops writing there.
- Backup capture and the server update rehearsal overlay include the artifact
  folder.

### Migration

- Reports move from SQLite to files through a storage migration.
- The migration runner gains a file-root input. A real run passes the data
  directory. `rcp migrate --check` rehearses on an in-memory copy of the
  database, so it passes a throwaway folder; without this, a check would write
  report files into the live data directory.
- The migration writes each file first, named by its digest, then records paths
  and drops the HTML column in one transaction. A crash between the two leaves
  only unused files, which the next start reuses.
- The server update rehearsal already runs on a copied data directory, so its
  files land in the overlay.
- Project transfer and backup archives written before this change stay
  importable: the importer still accepts report HTML inline.
- A new fixture joins the upgrade CI, per
  [every server-era schema remains directly upgradeable](../decisions/2026-08-27-server-schema-compatibility.md).

### Editing in place

- Every artifact is editable in place, reports included. There is no candidate,
  no Accept, and no Reject. **Undo** steps back one version. The original is
  never dropped.
- A comment is sent from the viewer. It always runs as the least-authority turn
  that can edit a file; there is no mode picker.
- The turn's shape follows the session the artifact came from:
  - **Chat session:** an ordinary Discuss turn. The chat master already defines
    Discuss.
  - **Experiment or Auto-research session** (reports and turn artifacts alike):
    a revoking launch, like today's report launch. It carries no master and
    only the edit instructions. The existing check that re-opens the master
    after a report widens from report attempts to every revoking launch.
- Admission decides the shape once, from the origin session. The prompt builder
  receives the launch kind and master from admission and does not branch again.
- RCP stages the current bytes at a writable path in the turn's scratch. The
  agent edits that file in place. When the turn ends, RCP reads that exact path
  back and publishes it as the next version. Any other file the turn writes is
  an ordinary new artifact.
- While an edit turn runs, Send waits and shows the existing unavailable
  reason. Comments do not steer and are not queued.
- The agent's reply goes to the same chat. The viewer shows only an Editing
  indicator and a control that brings the chat up.

### Live pages

- A live page is an HTML artifact that keeps redrawing after the turn ends,
  from data RCP sends while the human has it open. It is for something still
  changing: a training job, a sweep, a running episode, a node gathering
  Evidence. A result that is already final is an ordinary page.
- The page declares what it watches in one tag:
  `<script type="application/json" id="rcp-live">{"version":1,"needs":[...]}</script>`.
- Four kinds of need:
  - `job`, by the key the agent passed to `launch --key`, resolved against the
    jobs the artifact's own turn lineage launched;
  - `node`, by id in the artifact's project graph;
  - `episode`, the artifact's own episode, offered only for episode artifacts;
  - `file`, an absolute path inside the project's readable roots on the
    artifact's execution host, read as `tail` or `whole`, formatted as `jsonl`,
    `csv`, or `text`.
- RCP validates the tag when it discovers the artifact. An invalid tag leaves
  the artifact static with a visible notice.
- The trusted viewer frame fetches one snapshot for the artifact from one
  endpoint and sends it into the page as an `rcp-live-data` message. The page
  makes no network call. Invariant 10e is unchanged.
- The frame refreshes while the page is visible. `file` needs on an SSH host
  refresh more slowly and reuse RCP's existing connection.
- RCP stores the last snapshot it served. Once every `job` and `episode` need
  has finished, it reads one final snapshot and stops refreshing. The page then
  renders from that saved snapshot, even after the job's files are gone. A page
  that watches only nodes and files keeps refreshing while open.
- An episode report is never live.
- The allowed kinds, fields, and caps are one model in code. Caps live in
  `limits.py`.

### Prompts

- One artifact contract, rendered from the `Artifact` model, replaces the three
  near-identical copies in the Discuss, Work, and Experiment contracts. It says
  that an empty artifact directory is a normal result, states the viewable types
  and caps, and says how a commented artifact is edited in place.
- The contract carries a short live section rendered from the live-data model:
  what a live page is, when to use one, the declaration and message formats, and
  the fields of each kind.
- A static `live-pages` skill holds worked examples: a loss curve, a sweep grid,
  an episode progress bar. A test parses every example tag in the skill and
  validates it against the model.
- The shared reply rule no longer asks for a figure whenever a result has
  numbers. It asks for one when a figure is clearer than prose.
- Attachment lines drop "write the whole file to a new path" and "this is an
  episode report; it cannot be revised."
- Master and policy version constants stay as they are. Open sessions keep
  their existing master; the per-turn lines change for everyone.

### Viewer

- One in-app panel, built on the existing `DraggableWindow`, replaces the
  desktop's native preview window. Repository-file previews and report popups
  use it too. PDFs still open in the system viewer.
- There is no mode button row. Drag the left edge to resize, drag the title bar
  to float, double-click the title bar for full screen. The dock control
  collapses it to a slim tab at the top right of the RCP window.
- The title bar holds the name, the Live or Finished indicator, the version,
  Undo, a control that brings the chat up, dock, and close. All of it is RCP
  chrome outside the agent's HTML.
- Text and area selection, the comment box, and Send sit in the viewer.

## Implementation slices

1. **Storage and migration.** `Artifact` records and version folders, report
   migration with the file-root runner input, repository import, backup and
   overlay capture, archive import, legacy result-view removal.
2. **Editing.** Admission by origin session, staging through `Artifact`,
   publishing in the shared turn finish, the widened re-open check, removal of
   candidates and Accept/Reject.
3. **Prompts.** The rendered artifact contract and live section, the
   `live-pages` skill and its example test, attachment lines, the reply rule.
4. **Live data.** Tag validation at discovery, the snapshot endpoint for the
   four kinds, the stored final snapshot.
5. **Viewer.** The docked panel, removal of native preview windows, commenting
   and Send in the viewer, the title-bar controls.

Current behavior docs change with their slice:
`paper-artifacts-and-result-views.md`, `conversations-episodes-and-watchers.md`,
`interface-and-visual-design.md`, `api-web-and-desktop-projections.md`, and
`server-and-machine-operations.md`.

## Checks

- Focused pytest for storage, migration, admission, prompts, and the snapshot
  endpoint; web tests and `npm --prefix web run build`.
- The new upgrade fixture, plus `rcp migrate --check` on a copy of real data,
  confirming nothing is written beside the live database.
- A served-app journey on disposable data covering the closing conditions.
- A rebuilt desktop app, per `docs/desktop.md`, for the removed native window.

## Out of scope

- Metrics collection by RCP and direct integration with experiment trackers.
  A run that writes its metrics to a file is covered by `file`.
- A glob form of `file` for sweeps.
- A push channel from server to browser. The snapshot shape stays the same if
  one is added later.
- Queued comments.
