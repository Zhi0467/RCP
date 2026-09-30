# Live artifacts and the docked viewer

Date: 2026-09-29
Status: all five slices are implemented on one PR: storage and migration,
import and editing, prompts, live data, and the viewer. Current behavior lives
in [paper-artifacts-and-result-views.md](../specs/paper-artifacts-and-result-views.md)
and the specs it links. The rationale lives in
[Artifacts are RCP-owned and edited in place](../decisions/2026-09-29-artifacts-are-rcp-owned-and-edited-in-place.md).

What remains is verification that needs a served app with a real provider, a
packaged build, or real data. A browser check on disposable data covered the
panel's placement, Undo, Send, and version reload. The in-app browser cannot
render the sandboxed page itself.

Close this handoff when all of these hold on a served app with disposable data:

- A chat turn launches a helper job and writes a live loss curve. The page
  redraws while the job runs, stops refreshing when it ends, and still renders
  from its saved final data after the job's files are deleted.
- A comment sent from the viewer on a chat artifact comes back as the next
  version in the same viewer, in the artifact's own chat session. Undo returns
  to the previous version.
- The same holds for an Experiment turn's artifact and for an episode report.
  The next Experiment turn in that session re-opens its own master.
- In the rebuilt desktop app, the viewer docks, floats, resizes, and goes full
  screen inside the RCP window, and no native preview window opens. The checks
  are in [desktop.md](../desktop.md#artifact-viewer-checks).
- An update from `main` moves every stored report out of SQLite and imports
  existing artifacts. A team server update rehearsal passes on a copy of real
  data, and `rcp migrate --check` on that copy writes nothing beside the live
  database.
