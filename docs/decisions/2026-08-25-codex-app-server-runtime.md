# Codex app-server is a profile runtime, not a session owner

**Status:** accepted on 2026-08-25.

## Decision

RCP exposes provider runtime selection on each project agent profile. Codex
profiles offer `exec` and `app-server`; profiles without a saved value keep the
existing `exec` behavior. Provider profiles own the supported choices and their
implementations. Shared launch plumbing continues to own local versus SSH,
process control, staging, event delivery, and cleanup.

An app-server invocation is one fresh stdio process per RCP provider turn. The
current profile preference is consulted for each new task invocation, even when
that invocation resumes an existing provider thread. The actual runtime is
recorded per task invocation and on a Paper writing-session update; there is no
central provider-session/runtime binding table.

If app-server fails before the new prompt can have reached Codex, RCP silently
retries through exec on the same machine and with the same provider thread id,
capability, stage, and filesystem scope. RCP records exec as the actual runtime.
Once the app-server `turn/start` write begins, fallback is forbidden because the
prompt may already have been accepted and an exec retry could duplicate work.

## Why

Runtime is an execution preference, so the existing project profile is the
place where the human already chooses provider, model, reasoning, and machine.
Binding it permanently to a native session would make a configuration change
ineffective for existing conversations even though installed Codex can resume
an app-server-created thread through exec. A separate session table would also
duplicate identity already held by task, chat, episode, and Paper records.

The prompt-delivery boundary makes fallback both useful and honest. Startup,
version, and pre-turn protocol failures cannot have done the requested work;
post-`turn/start` failures might have, so they must fail without replay.

## Rejected alternatives

- An environment variable: not project-visible, not configurable in Settings,
  and easy for local and SSH behavior to diverge.
- An immutable runtime per provider session: prevents current profile policy
  from applying to ordinary resumption and requires a new identity table.
- Fallback after prompt delivery: can duplicate repository or graph-adjacent
  operational work.
- A persistent app-server owned across turns: expands recovery, ownership, and
  concurrency semantics and belongs to the still-open live-interruption question.
- Desktop takeover or sidebar ordering: Codex Desktop already renders persisted
  threads; RCP needs inspection visibility, not ownership of its UI.
