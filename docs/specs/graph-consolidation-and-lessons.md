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
writing rules, and defines shared terms in the glossary. Experiments get their
own check: one study per node, an objective and design that state the operation
(what runs, conditions, run matrix and count, what is logged) rather than the
record's history, defined internal names, and fields that agree. What it cannot
fix from sources, such as a node mixing two studies, becomes a Suggestion. It
checks every asserted Experiment closely and rewrites an accepted one only when
a reader could not tell what it runs, since the rewrite returns it to asserted
for another review; for other accepted nodes it only suggests rewrites. Existing protected beliefs still
require [Proposals](authority-and-proposals.md#protected-belief-proposals).

The workflow re-homes historical links using ordinary Patch verbs. It maps a
Question's Decision only to existing Hypotheses the choice serves, or keeps a
pure Experiment input connected through `governed_by`; it never invents a
Hypothesis for topology. It maps Question/Decision Blockers to the existing
Experiments they prevent from starting, and replaces a `requires_decision`
choice wrapper with a genuine Experiment input when supported. Ordinary edge
replacements can share a Patch; protected node changes still use Proposals.
If no supported replacement fits, it retains the old edge and reports its id
under Suggestions. Blocker resolution remains explicit and authorized, and
Decision ballots stay unchanged. Upgrade alone starts no cleanup: the schedule
still requires a new non-consolidation revision since the covered head.

Its charter is "Tidy and check sources; do not run experiments." After the
audit, a fact-check reads Evidence created or changed since the covered head
against the sources themselves: cited artifacts, producing Experiment job logs,
and repository files on the execution host, and papers or web pages through the
provider's web and network tools, never conversation transcripts.
The workflow invites provider-native subagents for independent pieces (audit by
question, fact-check by Evidence batch, literature search by question) and keeps
combining, the Patch, apply, and the report in the root turn. A fix the agent is
sure of, having read the full source and any relevant code or results, goes
directly in the Patch: prose, citations, a wrong number, or an omitted or
overstated result. Only what it cannot settle, such as a claim checked only
against an abstract, raises `overclaim`, `underclaim`, `no_source`, or
`unverified` flags, at most five a night: `no_source` means the sources read do
not contain the claim, `unverified` that a named source could not be read. The
cap is prompt guidance, not enforced. The workflow never changes the flagged
claim, validity, or standing to resolve such a finding.

The same pass then uses the turn's web search for recent work bearing on open
ResearchQuestions and Hypotheses, recording each query, date, source, and
whether the full text or only the abstract was read. It prefers changes to
existing records and Suggestions over new nodes. Qualifying an ordinary
Evidence edge uses removal and recreation; changing the protected belief itself
uses a Proposal. Ordinary consolidation cannot author a merge-only atomic
protected-edge replacement: it uses safe separately approved steps or leaves a
Suggestion. It may add Evidence for a result a source it read states, including
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
  Keeping from the viewer also closes the row. A Bulletin link saves the report
  but leaves the row open: saving is retention, and only a human Keep or Dismiss
  settles the review.
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
not a finding, and an `unverified` flag names a source no agent could read, so
both are flagged only: their rows have no **Address**, **Address all** skips
them, and the endpoint refuses them with `flag_not_addressable`. **Address** on
any other kind, `no_source` included, starts an ordinary member-authorized node
Work chat on that Evidence, with the default node-chat
provider profile and ordinary admission and prompt construction. Its first human
message combines the flag context with backend-authored instructions to read the
source, correct the Evidence only as far as the source supports, and use `ask`
when unsure. The flag grants no authority. Replaying the request or addressing an
already-addressing flag returns the existing chat without another turn. Address
leaves the member in the Inbox; the chat opens only from **Open chat**.

When at least two findings offer Address, the header offers **Address all**.
It sends one request for the ten oldest addressable findings, ordered by creation
time. The server caps each batch at ten and refuses empty or duplicate ids,
non-open flags, flag-only kinds, and absent or retired Evidence on main. One finding uses
the same node Work path as Address; two or more start one ordinary project Work
turn on main in one new chat. Its message labels every finding as context.
Sorted flag locks serialize admission, and every flag binds to that turn and
chat in the task's transaction or none do. Each addressing strip opens the shared
chat. The request key and exact ids survive a failed Web attempt; replay returns
that request's own turn even after settlement or a later attempt. Batch settlement
and Resume/Retry takeover apply to all flags still bound to the operation.

The Address turn's end settles the flag in the same transition that records it:
a turn that succeeds, reports its Work outcome as `done`, and has no rejected or
unavailable graph update closes the flag, so a member need not remember to
Dismiss it. Any other end, including a `partial` or `could_not` outcome, failure,
interruption, or Stop, reopens it with its chat kept, and a new Address starts
another turn. The chat's own Resume or Retry of that turn takes the flag back to
addressing, unless it was since dismissed or bound to another attempt. Startup
settles any addressing flag whose turn already ended, including flags an older
release left addressing, after following the binding to that turn's latest
Resume or Retry and moving any Address request id an older release
kept on the flag into the client request receipts replays read. While addressing, the row is a strip that only opens the chat. An
open row offers **Address**, **Open chat** after a failed attempt, **Open
Evidence**, **Open source** only when the source is a URL (opened in a new tab) or
resolves to a configured repository file, and icon-only **Dismiss**, which closes
it for all members. Open or addressing flags give the Evidence node a red
left edge and one icon-only flag action opening Inbox. Dismissed flags cannot be
addressed. Address also refuses when main's authoritative state, read at
admission, holds no current Evidence node with that id. Like any node chat, it
does not fence a transition that lands while the chat is being admitted.

## Operational lessons

Lessons are short operational notes in RCP's data directory, never graph state or
authority. They cannot widen capability, roots, target, or budget. A member's
ordinary Work or Discuss chat (`node_chat` / `project_chat`), including question
answers and watcher follow-ups, may add, list, update, and delete every lesson in
its project. Each command checks the authorizing human's live project membership.
Chat additions and edits are human-owned, attributed to that human's captured
user id and display name. Members also manage all lessons in Settings.

Child Work, Experiment turns, and Auto-research root/workers remain add-only.
Consolidation may add, list, update, and delete every note, human-owned included,
and lists each change in its report. Seed, Refresh, and branch merge have no lesson
commands. Discuss gains no graph or Apply authority. The handler's resolved
lesson actions also render its usage from the command parser's option records.

**Why:** a member's ordinary chat acts for that member on operational notes, and
the human prefers a small, current lesson set that the nightly run keeps pruned.

Agent mutations key by operation/subcommand/key, check live authorizer membership,
and commit with their durable result, so retries replay even after deletion.
Discuss, Work, Experiment, Auto-research, and consolidation launches receive a
pointer to staged `lessons.md`, rendered at staging from the store. It is bounded
by `LESSONS_RENDER_MAX_BYTES`, human-owned first, newest first within each group,
with an omitted count. Text/count caps are `LESSON_TEXT_MAX_CHARS` and
`LESSONS_PER_PROJECT_MAX`.

Operational know-how is not research meaning for replay, merge, or paper,
and repository instruction files belong to the human. The turn encountering a
problem records it while it has context; nightly work prunes it. A bounded file
pointer avoids spending prompt tokens on every lesson at every launch.

## Transfer, backup, and restore

Lessons, Evidence flags, schedules, runs, and receipts stay with the space,
outside project transfer. Backup captures them in SQLite. Restore retains lessons/closed rows,
fails unresolved runs, and disables schedules until a member enables them again.
