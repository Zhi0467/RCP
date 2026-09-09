# Claude queued follow-up verification handoff

Date: 2026-09-08
Status: implemented under the human-confirmed brief, with verification gaps
recorded below. The settled contract is Claude queued follow-up turns, Codex
injection, runtime-owned composer wording, honest first-error diagnostics, and cached Work-like sandbox
readiness. Remaining work is the external S134 drive and checks blocked by this
execution sandbox; no real-provider or SSH acceptance pass is claimed.

This replaces the materially different
[archived steering handoff](../archive/handoffs/handoff-2026-09-05-live-provider-steering.md).
Current behavior belongs in
[providers and containment](../specs/providers-and-containment.md#live-human-steering)
and the confirmed journey in
[S134](../acceptance/S134-steer-the-running-human-chat.md).

## Settled boundaries

- Claude lifecycle `started` establishes readiness; matching `queued` acknowledges
  a follow-up. Replay echoes and `queued_turn_count` establish neither.
- Placement is Claude's, not RCP's. Probed 2026-09-09 on 2.1.263: a message sent
  while a Bash call was running was `started` and `completed` inside the first
  turn and the single result attributed both UUIDs (`STEERED`); a message sent
  with no tool running was queued and ran as a second turn. Receipts say
  **Delivered** and the composer says **Send to the running turn**; nothing
  promises "after the current turn". Attribution settles the stop in both cases.
- Only RCP-generated outstanding command UUIDs may extend a process beyond its
  first result. Missing usable result UUIDs retain the stop fence.
- Follow-ups retain the running capability, scope, stage, and session. Answers
  join into one assistant message; Work reads one final `patch.json`.
- The first provider error includes meaningful stderr. Authenticated readiness
  probes Claude's Work sandbox with empty stdin before any model call. Discuss
  remains independent of that Work-like precondition.
- No new provider daemon, task, answer file, graph channel, credential change,
  automatic resend, or authority widening is authorized.
- Claude Work turns cannot reach the command broker, and a write allow-list
  cannot fix it. Probed on the bubblewrap host on 2026-09-09: inside the Work
  sandbox `socket.socket(AF_UNIX, SOCK_STREAM)` fails `EPERM` at creation, while
  `AF_INET` succeeds, so the denial is the socket family rather than the socket
  path. A grant was implemented, verified against the real host, observed not to
  help, and reverted. Changing the broker transport is open work with no
  decision taken; the spec records the current limitation.
- A succeeding turn records the usage its result reported. A labelled answer is
  withheld from the wire, so before this only a failing turn — whose error event
  is forwarded — was ever counted.

## Remaining acceptance drive

Use disposable data and spare ports. Complete S134 with real local and authorized
SSH providers: verify same-session Claude follow-ups through the served UI,
Codex injection, Work containment, transport loss without resend, and restart
with an unacknowledged message. Confirm both a working sandbox and a concrete
missing-sandbox readiness diagnostic on the actual execution host. Existing
probe facts supplied by the human are established evidence, not a new integrated
acceptance pass. Archive this handoff when that remaining drive is complete.

## Verification in this worktree

- Focused protocol, API, launcher, and documentation regressions pass. The real
  background task and fake CLI integration stores one assistant message joining
  both results, and preserves stderr in a failed task's human-visible error.
- A disposable served app on port 62215 with fake Claude verified a delivered
  receipt and one successful combined answer through HTTP. Its requests returned
  200/202; server logs contained only the unrelated browser favicon 404.
- Safari rendered the disposable project index, then human activity changed its
  active window. No further UI actions were taken. Composer pixels, browser
  console inspection, and the full live-provider UI drive remain unverified.
- Chromium cannot launch inside this macOS sandbox (`bootstrap_check_in`,
  Permission denied 1100). Watcher tests invoking `ps` are also denied. Exact
  final check counts and commands are in the root work summary. One episode
  acceptance timeout in the full parallel run passed with all six tests in its
  isolated file rerun; no episode code or timeout was changed.
- Usage forwarding is fixed in this branch, not left as a gap: `_stream_agent_events`
  now forwards an answer's usage on its own frame, and the regression test was
  checked against the unfixed code, where it fails with zero usage rows. The S134
  separate-usage assertion is exercised by the two-result protocol test; its
  live-provider drive is still part of the external S134 run.

The pending/blocked acceptance scan found no additional completed journey. S134
retains `blocked-external`; S35 still needs packaged/live-host verification, S60
owns wizard vocabulary, and S121 owns graph Apply refusal semantics.
