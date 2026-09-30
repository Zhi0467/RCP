# Artifacts are RCP-owned and edited in place

**Status:** accepted on 2026-09-29. Work in progress is tracked in the
[live artifacts handoff](../handoffs/handoff-2026-09-29-live-artifacts.md).

## Decision

Every artifact is one `Artifact` record whose bytes RCP stores in its own data
directory, with its original and a bounded number of later versions. A human
comment edits it in place and the result is the next version. A live page reads
data only through a one-way message from RCP's own viewer frame.

## Why RCP storage, not the repository

Keep used to write an `artifacts/` folder into the project's state repository,
which is one of the human's research repositories. That put an RCP-managed
folder beside the human's code. Git history was the only record of edits.

Once RCP keeps versions itself, the repository adds nothing an artifact needs.
Team members already share the team server's data directory. Artifacts do not
travel with a clone, and that is intended.

## Why no Accept step

Accept existed because an edit could overwrite a file the human might also be
editing in the repository, and because a revision was a separate Work turn. With
bytes held by RCP, nothing else edits them, and Undo reverses an unwanted edit
in one step. Reviewing a candidate before it shows cost a round trip on every
comment.

## Why the comment turn follows the session

A session's master contract decides what a turn may say. A chat master defines
Discuss, so a comment in a chat session is a Discuss turn. An Experiment or
Auto-research master does not. Rendering a Discuss turn there either names a
mode that master never defines, or swaps in the chat master, whose stage paths
belong to another stage and which nothing swaps back. A revoking launch, as the
report already uses, grants nothing beyond the edit, and the check that re-opens
the master afterwards already exists.

## Why live data flows one way

Agent HTML cannot reach RCP (invariant 10e). Letting a page fetch would mean
handing agent code a credential that reads project data. RCP's trusted viewer
frame fetches the declared data and posts it into the page. Letting agent code
run on each refresh to read a file would run it outside any launch; the agent
shapes the data in the code that produces it instead.

## Rejected alternatives

- **One record with a supplier switch in shared code.** Suppliers set their own
  rules at creation; shared code never branches on them.
- **Kept files under `.research/artifacts/`.** Still inside the human's
  repository.
- **Diff-based history.** Images do not diff, and agents usually rewrite the
  whole file.
- **Queueing comments during an edit.** RCP has no queue, and an edit turn is
  short.
