# One named branch

Status: design confirmed 2026-10-11, mockups approved; not started.
Decision: [a branch is one named object](../decisions/2026-10-11-a-branch-is-one-named-object.md).

## Settled

Picker

- A `⑂ <name> ▾` button at the right end of the view-tabs strip replaces the
  Graph strip. It is hidden when the project has no live branch and you are on
  main. On a phone it is a chip in the top bar, beside the project name.
- The button shows the current name and an amber dot when another branch has
  something waiting for you. It shows no spinner.
- The list: `main` first, then branches with a running agent, then newest
  first. Each row shows its name, a spinner when an agent runs there, an amber
  dot when it has open Decisions, open blockers, or an unanswered agent
  question, and pencil and archive icons that are always visible. The current
  row has a check.
- Pencil turns the name into inline text at the same size; Enter or the check
  saves, Esc or the cross cancels. An invalid or taken name shows one warning.
  `main` has no pencil or archive.
- Archive on an unmerged branch asks "Archive <name> without merging?" first.
  Archived branches are not listed. A Runs card opens its archived branch;
  while you are on it the row shows unarchive in place of archive.
- No revision, merge state, or run link in the picker. Merge stays on Runs.

Names

- Lowercase letters, digits, and hyphens, 2–32 characters, unique within the
  project, never reused, never `main`.
- One shared branch field, used by both launches. Auto-research shows it in
  the row where "Work on a graph branch" was, beside Code worktree. The
  Experiment Run section renames its toggle to "On a new branch", with the same
  field beside it. Blank means `research-<mon>-<day>` or
  `experiment-<mon>-<day>`, with `-2` on a clash. No hint text; a wrong name
  shows one warning.
- Old branches get the same date names, computed, not migrated.

On a branch

- The Auto-research button opens "Continue <name>": a message to the
  orchestrator while it runs, else Add N turns with that message (the existing
  ended-orchestrator continuation). No branch from a branch.
- The Experiment Run section hides "On a new branch"; the Experiment runs on
  the branch you are on.

Views

- Overview, Inbox, Research, and Agents follow the selected branch.
- Agents keeps its four columns and cards and drops the graph filter and the
  branch and role tags on cards. The user tag stays.
- Runs and Artifacts stay project-wide. Every row carries a `⑂ <name>` tag,
  `main` included.

Switching

- A switch never shows the loading screen. The page stays; a thin progress
  line runs under the view tabs until the branch's snapshot paints.
- Live branch snapshots load in the background when a project opens, so a
  switch is usually instant. A revisited branch paints from its kept copy.
## Reload causes

Probed 2026-10-11 by logging every run of the project-open effect in
`App.tsx` and which dependency changed. In a personal space, view switches,
branch switches (first visit, revisit, from Agents), idle heartbeats, and a
server restart caused no cold open. Every cold open comes from one shape: the
effect reruns while the project is already on screen and takes the cold path,
which sets `loading` and replaces the whole page with "Opening project" until
a full remote read returns. The paths into it:

1. **Identity re-check during a branch switch.** A re-check flips
   `backendSessionReady` off and on. The resume path then requires the session
   to still hold a project, but a branch switch empties the session until the
   new snapshot arrives, so it cold-opens. Re-checks come from: a request that
   never reaches the server in a team space (transport recovery), any failed
   mutation, an active-task poll failure or its recovery, the desktop window
   being shown again (`prepare-show`), a backend mismatch event, and update
   apply. On a team server the first branch read is slow, which widens the
   window. This matches the reported 5–6 s.
2. **Identity re-check during any first open or project-tab switch** with no
   tab cache: the same empty-session condition, without a branch involved.
3. **A changed backend** (team server restart or update: new `instance_id`)
   clears the project by design and cold-opens it.
4. **Any other dependency change** of the effect while a project is open
   (about 25 inputs, mostly callbacks) is treated as a fresh open.

Fix: ref activation leaves the open effect and becomes a project-session
action. The open effect keeps the page whenever the project on screen is the
one being opened, for every path above including a changed backend: the page
stays inert under the thin progress line, and only the first open of a
project from the index may show the loading screen. The resume path accepts a
held snapshot of the same project. The Checks below drive each trigger.

## Also in this PR

- Project Settings opens with every machine card closed (Space settings
  already did).
- The composer's Compute toggle moves into the **+** menu beside files and
  artifacts; mockup first.

## Slices

Each slice updates the specs it changes and adds tests in proportion.

**A. Backend names and refs.** One owner for branch names: a store table keyed
by project and branch, validation, the date fallback, uniqueness, and
never-reuse. `graph-refs` returns `name`, `running`, `needs_you`, and
`archived`, and drops `title`. Routes: rename, archive, unarchive. Auto-research
and Experiment start requests take an optional `branch_name`, reserved in the
same transaction as the episode. Transfer carries names. Task, artifact, and
Runs projections name the branch by this owner.

**B. Picker and switching.** Button, list, rename, archive confirm, phone chip.
Ref activation as a session action, background snapshot loading, progress
line, and the loading-screen rule.

**C. Launches.** The shared branch field in both launches; the Continue dialog
on a branch; the hidden Experiment toggle on a branch.

**D. Views.** Agents per branch without filter or tags; branch tags on Runs and
Artifacts; voice and WebMCP reads use names.

A first; B, C, and D in parallel after A.

## Checks

- Python: name validation, fallback, uniqueness, never-reuse, rename and
  archive routes, `branch_name` on both starts, refs fields, transfer.
- Web: picker order and markers, rename and archive flows, Agents filtering,
  tags by name, no loading screen across a switch with chats loaded.
- Served drive on a throwaway team server with branches and chats: switch from
  Agents with a chat open, from Overview, and back; network shows no project
  reopen, the console is clean, and `.app-loading` never appears.
- Specs: `api-web-and-desktop-projections.md`, `auto-research-and-branch-merge.md`,
  `interface-and-visual-design.md`.
