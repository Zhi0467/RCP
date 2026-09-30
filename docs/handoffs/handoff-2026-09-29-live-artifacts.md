# Live artifacts and the docked viewer

Date: 2026-09-29
Status: design settled with the human on 2026-09-29, then revised the same day
after an xhigh design review and the human's answers to its two open
questions. Slice 1 is implemented in this worktree: version storage, report and
legacy-view migration, turn discovery including child Work recovery, and typed
backup/transfer inventories. Slice 2a is implemented: per-project background
import of legacy local, SSH, and repository-kept bytes; ordinary artifacts for
unresolved candidates on their own turns; durable failure reasons and retry
backoff; and one SQLite snapshot for transfer artifact metadata and inventory.
Imported temporary artifacts expire at the source stage's last-touch time plus
the existing retention period; kept artifacts have no expiry. Import never
accepts a candidate or removes repository-kept files. Slice 2b is implemented:
comments edit staged files under Discuss or revoking scratch-only authority,
admission reserves sessions and stages across launch owners, publication uses
version compare-and-set with retry identity, Undo moves back a version, and
candidate creation and Accept/Reject are removed. Reply destinations follow the
settled episode rule below. Explicit fresh sessions retain those destinations.
Slices 3 and 4 are implemented: the rendered artifact contract and live
section, the `live-pages` skill, per-version source resolution for discovered
and edited versions, the snapshot endpoint and shell relay, and server-saved
final snapshots. Slice 5 remains open. Docs and code land in one PR.

Slice 1 verification still needs the installed Linux coordinator transition and
a rehearsal on a copy of real team data. Local storage, route, recovery, and
archive tests cover the implemented paths. Browser automation is blocked by
macOS Chromium launch permissions in this environment.

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
- an update from `main` moves every stored report out of SQLite and imports
  existing artifacts, and a team server update rehearsal passes on a copy of
  real data.

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

- One `Artifact` class replaces task artifact descriptors, the byte storage of
  episode reports, and legacy result views.
- It has two suppliers. A **turn** (chat, Experiment, or Auto-research worker)
  makes artifacts in its artifact directory. An **episode ending** makes one
  report. The supplier's own code sets the artifact's rules when it creates
  the record: a turn artifact expires unless kept; a report never expires.
- The record stores its supplier as data. Viewing, editing, versions, and live
  data never branch on it.
- The episode report record stays the lifecycle proof for its attempt,
  allocation, ending, and episode. It keeps an immutable binding to the
  report's first artifact version instead of holding HTML. Later versions
  belong to the artifact. An edit never rewrites the first version or
  resettles the episode.
- Result views were already retired as a creatable kind. Their legacy rows
  migrate into `Artifact` records. Their `result_view` request plumbing and
  prompt section are deleted. Their legacy URLs stay as thin aliases to the
  artifact routes, keeping the compatibility promise without a second
  implementation.

### Storage

- Artifact bytes are files under the RCP data directory, one folder per
  artifact. SQLite holds only metadata and refers to files by identifiers
  relative to that folder, never by absolute path, so a relocated or copied
  data directory still resolves.
- A turn's artifacts are copied into that folder when RCP discovers them, within
  the existing per-turn count and size caps. Auto-research worker turns get an
  artifact directory today but are never discovered; they now go through the
  same discovery, including retry and recovered turns, and their artifacts show
  on the episode timeline's turn popover. Viewing no longer proxies from a
  local or remote stage.
- Each artifact keeps its original plus its last N versions, capped by total
  bytes. N and the byte cap live in `limits.py`.
- **Keep** now only stops expiry. RCP no longer writes an `artifacts/` folder
  into the state repository.
- Backup and transfer carry one typed artifact inventory: every version and
  saved live snapshot the database refers to. Capture copies exactly those
  files, and pruning cannot remove a file while a capture holds it. Export,
  import, restore, schema-digest checks, and relocation all read that inventory.

### Migration and import

- Two separate steps. A storage migration changes only what SQLite and the data
  directory already hold. A background import brings in bytes that live
  elsewhere.
- **Storage migration.** Reports move from SQLite to files. The migration
  runner gains a file-root input: a real run passes the data directory, and
  `rcp migrate --check`, which rehearses on an in-memory copy of the database,
  passes a throwaway folder. The migration writes each file first, named by its
  digest, then records identifiers and drops the HTML column in one
  transaction. A crash between the two leaves only unused files, which the next
  start reuses.
- **Background import**, per project, resumable, and never blocking startup or
  other projects:
  - existing temporary artifacts, read from their local or SSH stage;
  - files already kept in the state repository's `artifacts/` folder, which
    stay in place;
  - each unresolved revision candidate, imported as an ordinary new artifact on
    its own turn, never as an accepted version.
  Each keeps its identity and expiry. A source that cannot be read, such as an
  offline SSH host, is recorded as unavailable and retried; the artifact shows
  that reason until import succeeds.
- **Server update rehearsal.** The installed release's coordinator migrates the
  copied database before it prepares copied repositories, then validates
  path-bearing columns. The new columns hold relative identifiers, and the
  migration never reads repositories. The PR tests the exact transition from
  the current release's coordinator to the new candidate.
- Archives written before this change stay importable: the importer accepts
  report HTML inline and kept files under the old repository location.
- A new fixture joins the upgrade CI, per
  [every server-era schema remains directly upgradeable](../decisions/2026-08-27-server-schema-compatibility.md).

### Editing in place

- Every artifact the viewer can show is editable in place, reports included:
  HTML, images, SVG, Markdown, and text, data, and code files. Selection
  gestures stay limited to HTML, images, and SVG; other types get a plain
  comment box. PDFs and download-only files are not editable.
- There is no candidate, no Accept, and no Reject. **Undo** steps back one
  version. The original is never dropped.
- A comment is sent from the viewer. It always runs as the least-authority turn
  that can edit a file; there is no mode picker.
- The turn's shape follows the contract the origin session actually holds, read
  from its recorded master, not from the artifact's supplier or episode:
  - **A session holding the chat master** (ordinary chats and Auto-research
    workers, whose master is the chat master plus a child boundary): an
    ordinary Discuss turn.
  - **A session holding an Experiment or Auto-research orchestrator master**
    (Experiment turn artifacts and reports): a revoking launch, like today's
    report launch. It carries no master and only the edit instructions.
- Admission decides the shape once, where it already resolves the origin
  session. The prompt builder receives the launch kind and master from
  admission and does not branch again.
- **One launch per session at a time.** Admission reserves the exact native
  session and stage atomically across every launch owner: chat turns, episode
  invocations, watcher wakes, recovery, reports, and edits. While any of them
  holds the session, Send waits and shows the existing unavailable reason.
  Comments do not steer and are not queued. An edit spends no episode budget
  and passes the episode's Stop fence untouched.
- **Re-opening the master.** The existing check that re-opens the master after
  a report widens to every revoking launch, and its clearing query counts only
  operational launches, so a finished edit never clears the requirement.
- **Publishing a version.** Admission records the artifact's current version as
  the edit's base. RCP stages those bytes at a writable path in the turn's
  scratch, and the agent edits that file in place. When the turn ends, RCP
  reads that exact path back, writes the bytes as an immutable file, then
  advances the version pointer only if it still equals the base, keyed by the
  operation so a retry cannot publish twice. Undo, Keep, expiry, and pruning
  take the same per-artifact lock. If the base moved, for example because
  another member pressed Undo, the new bytes become an ordinary new artifact
  on that turn instead of overwriting. A recovered or retried edit reuses its
  staged file rather than fetching current bytes again. Any other file the
  turn writes is an ordinary new artifact.
- **An origin that cannot resume.** Projects moved from a personal to a team
  space keep their tasks as history only, with no session or stage to resume.
  Their artifacts stay viewable, and the viewer offers an explicit **Edit in a
  new session** action. RCP never restores old execution authority to reach
  the original session.
- The agent's reply goes to the thread the artifact already belongs to. RCP
  resolves it through the artifact's episode, never from whichever view is
  open:
  - a chat artifact, or an Auto-research worker artifact: its own chat;
  - an Experiment episode's artifact or report: the Experiment node chat the
    episode runs in;
  - an Auto-research orchestrator artifact or report: the episode's
    orchestrator thread on its Runs card, where the human's comment appears as
    a message to the orchestrator and the reply appears beside it.
  The viewer shows only an Editing indicator and a control that brings that
  thread up.

### Live pages

- A live page is an HTML artifact that keeps redrawing after the turn ends,
  from data RCP sends while the human has it open. It is for something still
  changing: a training job, a sweep, a running episode, a node gathering
  Evidence. A result that is already final is an ordinary page.
- The page declares what it watches in one tag:
  `<script type="application/json" id="rcp-live">{"version":1,"needs":[...]}</script>`.
- Four kinds of need:
  - `job`, by the key the agent passed to `launch --key`;
  - `node`, by id in the artifact's own graph target, main or branch;
  - `episode`, the artifact's own episode, offered only for episode artifacts;
  - `file`, an absolute path inside the project's readable roots on the
    artifact's execution host, read as `tail` or `whole`, formatted as `jsonl`,
    `csv`, or `text`.
- **Resolution is stored per version.** When RCP discovers an artifact, or
  publishes an edited version, it validates the tag and resolves each need:
  job keys to stable job ids through the existing command receipts, which now
  record the key-to-job binding and reuse the existing lineage calculation;
  nodes to the artifact's graph target; files to host and root. The resolved
  needs are stored with that version. An invalid tag leaves the version static
  with a visible notice.
- **The snapshot endpoint takes only an artifact and version.** It never
  accepts paths or needs from the caller. On every read it rechecks project
  membership and graph target, and opens files through the existing reader
  that refuses symlinks and nonregular files.
- **The authenticated outer viewer shell fetches the snapshot** and relays it
  through the existing artifact message channel into the sandboxed page as an
  `rcp-live-data` message. The page itself makes no RCP request. Invariant
  10e is unchanged.
- The shell refreshes while the page is visible. `file` needs on an SSH host
  refresh more slowly and reuse RCP's existing connection.
- **The final snapshot is saved by the server.** When every `job` and
  `episode` need of a live version has ended, RCP reads one final snapshot and
  stores it with that version, whether or not anyone has the page open. It
  retries across SSH outages and marks a capture it could not complete as
  incomplete, never as final. After that the page renders from the saved
  snapshot, even once the job's files are gone. A page that watches only nodes
  and files keeps refreshing while open and has no final snapshot.
- **What a page receives, it can send out.** The sandbox still lets the page's
  scripts navigate their own frame, and Chromium does not enforce the CSP rule
  that would block it. RCP accepts this for the sources a page declares, as it
  already does for anything an agent writes into an artifact. The spec states
  this plainly and never claims the page makes no network request.
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
- Master and policy version constants stay as they are, by the human's choice.
  A session keeps the master it already holds, so open sessions learn the new
  contract, including live pages, only when a new session starts. The
  per-turn attachment lines change for everyone.

### Viewer

- One in-app panel, built on the existing `DraggableWindow`, replaces the
  desktop's native preview window. Report popups use it too. Repository-file
  previews move into it; their script-free route allows same-origin framing,
  while agent HTML stays opaque. PDFs still open in the system viewer.
- There is no mode button row. Drag the left edge to resize, drag the title bar
  to float, double-click the title bar for full screen. The dock control
  collapses it to a slim tab at the top right of the RCP window.
- The title bar holds the name, the Live or Finished indicator, the version,
  Undo, a control that brings the chat up, dock, and close. All of it is RCP
  chrome outside the agent's HTML.
- Selection, the comment box, and Send sit in the viewer.
- **A run's card lists every artifact from that run.** Today the Runs card
  shows only the final report, and artifacts from the episode's turns are
  found only in chat. The card lists the report and every artifact its
  Experiment turns and Auto-research worker turns produced, each opening in the
  viewer.

## Implementation slices

1. **Storage and migration.** `Artifact` records and version folders, the
   report binding and migration with the file-root runner input, the typed
   archive inventory for backup, transfer, and restore, legacy result-view
   aliases.
2. **Import and editing.** The background import, admission by recorded master,
   the session reservation, versioned publishing, the widened re-open check,
   the fresh-session action, removal of candidates and Accept/Reject.
3. **Prompts.** The rendered artifact contract and live section, the
   `live-pages` skill and its example test, attachment lines, the reply rule.
4. **Live data.** Per-version need resolution, the key-to-job receipt binding,
   the snapshot endpoint, the shell relay, server-side final snapshots.
5. **Viewer.** The docked panel, removal of native preview windows, repository
   preview framing, commenting and Send in the viewer, the title-bar controls,
   every run artifact on its Runs card.

Current behavior docs change with their slice: `docs/design.md`,
`paper-artifacts-and-result-views.md`, `conversations-episodes-and-watchers.md`,
`providers-and-containment.md`, `interface-and-visual-design.md`,
`api-web-and-desktop-projections.md`, `projects-spaces-and-operations.md`, and
`server-and-machine-operations.md`.

## Checks

- Focused pytest for storage, migration, import, admission, publishing races,
  prompts, and the snapshot endpoint; web tests and `npm --prefix web run build`.
- The new upgrade fixture; `rcp migrate --check` on a copy of real data,
  confirming nothing is written beside the live database; and the rehearsal
  from the current release's coordinator to the candidate.
- A served-app journey on disposable data covering the closing conditions.
- A rebuilt desktop app, per `docs/desktop.md`, for the removed native window.

## Out of scope

- Metrics collection by RCP and direct integration with experiment trackers.
  A run that writes its metrics to a file is covered by `file`.
- A glob form of `file` for sweeps.
- A push channel from server to browser. The snapshot shape stays the same if
  one is added later.
- Queued comments.
