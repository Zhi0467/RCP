# Graph consolidation and lessons

This spec owns nightly consolidation, its schedule and Inbox rows, and the
project's operational lessons.

## Authorization and schedule

A member enables consolidation in Project settings with local time/time zone.
The schedule captures member, choice time, and a fresh authorization id expiring
`CONSOLIDATION_AUTHORIZATION_DAYS` later. Choosing again renews it under the acting
member, even with unchanged settings. Unlaunched runs under the old id fail.
Any member may disable it; expiry stays visible until renewal or disablement.

Each local date is one occurrence. A nonexistent DST time uses the first valid
minute afterward; an ambiguous time uses its first instance. Polling uses
`CONSOLIDATION_POLL_SECONDS` after the machine's automatic-launch wake delay.
Occurrence handling, next due time, and run creation commit together. The next
occurrence is after handling time, collapsing missed dates into one run.

A due occurrence stays owed while an earlier run is unresolved and starts nothing
if authorization expired. Departed authorizers or projects refusing new work
produce failure rows. Paused resumable chat, unavailable history, and unresolved
launch configuration fail without tasks as `consolidation_chat_paused`,
`history_unavailable`, and `consolidation_launch_unavailable`; they advance the
occurrence, not coverage. No new main revision outside consolidation since the
covered head records `skipped` for recent nights. Otherwise one run/turn is admitted.

Only success advances the covered head: its input head plus subsequent revisions
from that run's operation, never concurrent work by others. Failure leaves it for
the next night. Immediately before first provider launch, including sign-in release
or startup recovery, RCP rechecks authorization id, expiry, membership, and write
admission. Failure launches nothing. Already-started turns settle under captured
authority; Apply still checks live membership.

## The consolidation turn

One ordinary Work turn on main uses `work_auto`, trigger `schedule`, and the
schedule authorizer. Its server-owned run binding grants consolidation behavior;
a chat id, workflow, or request field cannot. It uses the dedicated chat with no
native session from earlier nights, retaining normal scratch/artifact folders.
The official `graph-consolidation` workflow and dependencies resolve through the
skill registry regardless of project defaults and appear in the launch receipt.

The command client offers `validate`, keyed `apply`, `lesson`, and `flag`. Watcher handoff
is refused; ordinary Patch correction applies. Keyed Apply commits in-turn and
returns its revision, storing immutable bytes/digest under operation/key. Identical
bytes replay the result; changed bytes under a used key refuse. Another key with
the same bytes shares the canonical effect. At settlement, a matching final
`patch.json` is not reapplied; a new Patch follows ordinary Work Apply.

The workflow consolidates and rewrites unreadable asserted nodes under graph
writing rules, and defines shared terms in the glossary. It only suggests accepted
node rewrites, since editing resets standing. Existing protected beliefs still
require [Proposals](authority-and-proposals.md#protected-belief-proposals).

Its charter is "Tidy and check sources; do not run experiments." After the
audit, a fact-check reads Evidence created or changed since the covered head
against the sources themselves: cited artifacts, producing Experiment job logs,
and repository files on the execution host, and papers or web pages through the
provider's web and network tools, never conversation transcripts.
Provider-native subagents may check batches in parallel inside the turn. Prose
problems and missing or wrong citations are fixed directly in the Patch when
the right source is found. Scientific or semantic problems raise `overclaim`,
`underclaim`, `no_source`, or `unverified` flags: `no_source` means the sources
read do not contain the claim, `unverified` that a named source could not be
read. The workflow never changes the flagged claim, validity, or standing to
resolve such a finding.

The same pass then uses the turn's web search for recent work bearing on open
ResearchQuestions and Hypotheses, recording each query, date, source, and
whether the full text or only the abstract was read. It prefers changes to
existing records and Suggestions over new nodes; qualifying an existing
Evidence edge to a protected belief, or undermining that belief, goes through
a Proposal. It may add Evidence for a result a source it read states, including
`external_publication` Evidence with assessed edges, and fact-checks the
Evidence it added after applying. Its authority is otherwise unchanged.

It finishes with `consolidation-report.html` in the ordinary artifact root,
listing flags raised alongside applied changes. A **Literature** heading lists
every literature change, new paper, search record, and access gap, and says
when nothing new was found.

**Why:** expiring standing authorization makes routine maintenance possible while
bounding each night to one invocation. Ordinary Work on main avoids a new episode
kind and daily merge obligation; existing authority and semantic merge rules
already handle protected changes and open-branch conflicts. In-turn Apply lets
the report describe committed changes rather than intended ones.

## Outcomes and Inbox rows

Run outcome is separate from task verdict. Settlement uses run id, ended task,
canonical commits for its operation, and captured artifact. Startup and scheduler
passes retry until recorded. Success requires a succeeded task, no rejected or
unavailable graph update, and a viewable HTML report. Settled outcomes are final:
later history recovery can verify revisions/Proposal counts but cannot convert
failure to report or advance coverage.

Paused, stopped, abandoned, or irrecoverably interrupted runs fail without coverage
and never resume/retry/continue as scheduled runs. Humans may start separate Work
in that chat. Each run produces one **Consolidation** Inbox row:

- **Report:** opens in the viewer, offers Keep into Artifacts or Dismiss. Open
  rows protect temporary artifacts from expiry; Dismiss starts ordinary retention.
  Keeping from the viewer also closes the row.
- **Failure:** typed error and Dismiss only. Prelaunch failures have no operation
  or revisions. Postlaunch failures list canonical operation revisions, marked
  unverified if history cannot be read, never falsely empty.

Both show created Proposal counts. Keep/Dismiss close the row for all members,
using projected `can_write` independently of graph availability. Artifact Keep
uses `kept_at`, separate from expiry protection. Proposals use the ordinary Inbox;
report rows grant no authority. Each row emits one `consolidation` notification,
as does authorization first observed within `CONSOLIDATION_EXPIRY_NOTICE_DAYS` of
expiry or already expired. Closed rows and renewed/deleted grants invalidate notices.

## Evidence to check

Evidence flags are SQLite attention records, never graph state. Only the
consolidation run's mailbox offers and accepts
`flag --key K --evidence ID --kind overclaim|underclaim|no_source|unverified
--finding TEXT [--source PATH[:LINE]|URL]`. Prompt and handler share the owner's resolved verb set.
The id must name a current, non-retired Evidence node on main. The finding is
non-blank, single-line, and at most 400 characters; an optional source is at most
1000 characters and must name an absolute host path or `<alias>/<relative path>`,
optionally followed by `:LINE`, or an `http(s)` URL; anything else is refused.
Keys starting with `rcp:` are reserved for RCP.

RCP itself raises a `consolidation_added` flag on every current Evidence node an
applied consolidation Patch created, read from that Patch's apply receipt rather
than a graph diff, keyed `rcp:consolidation_added:<id>` and carrying the Patch
summary as its finding, so added Evidence waits for a human. Holds rerun after
every successful or replayed `apply` and when the run settles, whether its turn
succeeded or failed; their keys make that idempotent, so an interrupted hold is
recovered. A run whose applied Patches cannot be checked against canonical state
stays unsettled until a later pass can. The agent cannot raise that kind. A
failure to record it is a task warning; the applied Patch stands.

Flag creation checks live authorizer membership and records its keyed receipt
transactionally. The same key and arguments replay; changed arguments refuse.
There is at most one open or addressing flag per project, Evidence id, and kind;
another creation returns that flag with `code: flag_exists`. Each flag captures
the main revision, consolidation run and occurrence, and creation time.

Inbox's **Evidence to check** section lists open and addressing flags with kind,
Evidence title, and one-line finding. A `consolidation_added` flag is a notice,
not a finding: its row has no **Address**, and the endpoint refuses it with
`flag_not_addressable`, because there is nothing for an agent to correct.
**Address** on any other kind starts an ordinary
member-authorized node Work chat on that Evidence, with the default node-chat
provider profile and ordinary admission and prompt construction. Its first human
message combines the flag context with backend-authored instructions to read the
source, correct the Evidence only as far as the source supports, and use `ask`
when unsure. The flag grants no authority. Replaying the request or addressing an
already-addressing flag returns the existing chat without another turn.

The row then offers **Open chat**, **Open Evidence**, **Open source** only when the
source is a URL (opened in a new tab) or resolves to a configured repository file, and icon-only **Dismiss**. Addressing does not close it; Dismiss
closes it for all members. Open or addressing flags give the Evidence node a red
left edge and one icon-only flag action opening Inbox. Dismissed flags cannot be
addressed. Address also refuses when main's authoritative state, read at
admission, holds no current Evidence node with that id. Like any node chat, it
does not fence a transition that lands while the chat is being admitted.

## Operational lessons

Lessons are short operational notes in RCP's data directory, never graph state or
authority. They cannot widen capability, roots, target, or budget. Served mutating
mailbox owners may `lesson add`: human Work, child Work, Experiment turns,
Auto-research root/workers, and consolidation. Validation-only Discuss, Seed,
Refresh, and branch merge clients cannot. Consolidation may also page `lesson list`
and update/delete agent-written notes. Member-written or edited notes are human-owned
and agents cannot revise/delete them; members manage all lessons in Settings.

Agent mutations key by operation/subcommand/key, check live authorizer membership,
and commit with their durable result, so retries replay even after deletion.
Discuss, Work, Experiment, Auto-research, and consolidation launches receive a
pointer to staged `lessons.md`, rendered at staging from the store. It is bounded
by `LESSONS_RENDER_MAX_BYTES`, human-owned first, newest first within each group,
with an omitted count. Text/count caps are `LESSON_TEXT_MAX_CHARS` and
`LESSONS_PER_PROJECT_MAX`.

**Why:** operational know-how is not research meaning for replay, merge, or paper,
and repository instruction files belong to the human. The turn encountering a
problem records it while it has context; nightly work prunes it. A bounded file
pointer avoids spending prompt tokens on every lesson at every launch.

## Transfer, backup, and restore

Lessons, Evidence flags, schedules, runs, and receipts stay with the space,
outside project transfer. Backup captures them in SQLite. Restore retains lessons/closed rows,
fails unresolved runs, and disables schedules until a member enables them again.
