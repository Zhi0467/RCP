# Queue or steer in project threads

This extends a project thread's Send. Today it either steers the running turn or
waits. After this, it steers the running turn or queues the next turn, and the
thread shows what is waiting. Space threads (Pip) keep today's behavior. The
reported gap is on project threads, and Space threads compete with handoff
wakes, which a queue would also have to order.

## Problem

A chat holds one active turn. `_has_active_chat_overlap` refuses a second task
while one is queued, running or pausing. So `AgentThreadView` disables Send
("Wait until this turn ends") unless the active turn is running and
steerable. Two waits are invisible:

- A handoff from the member's Space thread waits in `agent_handoffs` while the
  target is busy. No route lists it.
- A delivered handoff turn that has not started shows only a "Waiting for the
  background worker" row. The member cannot steer or send.

## Behavior

- **Queue.** The composer offers Queue in two cases: while the thread has an
  active turn, or while waiting items exist. A queued message is stored on the
  backend. It starts later as its own ordinary member turn, with
  `trigger: "human"` and no `source`. Its text counts as a composer message for
  schedule creation (conversations spec, "Private agent threads").
- **Enter.** On a running, steerable turn, Enter steers and Queue is the second
  button. In every other case where Queue is offered, Enter queues. That
  includes an idle thread with waiting items, so a new message never jumps the
  queue. A paused turn keeps today's "Resume or retry the last turn first".
- **Show.** Above the composer there is one quiet row per waiting item:
  - a ready handoff: Handoff chip, Space agent name, message;
  - each of the member's queued messages, with a remove button;
  - a refused queued message, with its reason and Dismiss. Dismiss puts the
    text back in the draft.

  A handoff appears once its Space turn succeeded, which is when it becomes
  deliverable. Rows are ordered by when they became ready.
- **Order.** Handoffs are delivered before queued messages, and each kind is
  first in, first out. Nothing orders the two kinds against each other.
- **Remove.** The member removes their own unstarted message. A handoff is not
  removable here; it belongs to the Space turn that sent it.
- Attachments and artifact comments still need an idle thread with nothing
  waiting, as they do today.

## Data

The new table `agent_thread_queue` lives in `storage/agent_threads.py`, beside
`agent_handoffs`. It does not extend `agent_handoffs`, because that table needs
a source operation and drives settlement and the Space wake. A queued message
has none of these, and the cross-cutting rules forbid a kind selector.

| column | meaning |
|---|---|
| `queue_id` | primary key, uuid4 |
| `thread_id`, `user_id` | a project thread owned by the member |
| `idempotency_key` | UUID4 header value. Unique per `(thread_id, idempotency_key)`. A reused key with a different message is refused (422) |
| `message` | at most `AGENT_THREAD_QUEUE_MAX_CHARS` (16 000) |
| `created_at` | order; ties break by `queue_id` |
| `operation_id` | set when its turn is claimed |
| `refusal_reason`, `deferred_at` | final refusal; first transient deferral |

- Migration `(75, "agent_thread_queue_v1")` runs `migrate_agent_thread_queue`.
  It creates the table and the pending index
  `(thread_id, created_at) WHERE operation_id IS NULL AND refusal_reason IS NULL`.
  It also installs `install_agent_thread_version(connection,
  "agent_thread_queue", "ROW.user_id")`, so every row change bumps the member's
  `agent_threads` watermark.
- Limits in `limits.py`, beside the handoff limits:
  `AGENT_THREAD_QUEUE_MAX_CHARS = 16_000` and
  `AGENT_THREAD_QUEUE_MAX_ITEMS = 20` per thread (over the limit returns 409).
  The transient wait reuses `AGENT_HANDOFF_DELIVERY_TIMEOUT_SECONDS`.
- Remove and Dismiss delete the row.

Each owner that handles `agent_handoffs` today also handles the new table:

| owner | site | rule |
|---|---|---|
| restore | `restore_detachment.py` | delete unstarted rows (they would launch turns on the restored host) |
| transfer | `transfer/archive.py` `TRANSFER_GLOBAL_TABLES` | include the table |
| member removal | `members.py` `_remove_member_agent_data` | delete the member's rows |
| project deletion | `projects.py` | delete rows of the project's threads; add a count |

## Claim and admission

A new module, `runs/agent_thread_queue.py`, owns the drain.
`admit_queued_message(store, catalog, background, row, project_admission)` is
modeled on `_admit_delivery`.

1. **Pre-check.** A final refusal sets `refusal_reason` with
   `UPDATE … WHERE operation_id IS NULL`. The cases are:
   - the thread is missing, closed or archived (`chat_turn_refusal`);
   - the member is no longer a project member;
   - the Space user is missing, removed or being removed;
   - the display name is blank.

   A paused turn waits. It is not a refusal.
2. **Identity.** `AuthorizedHuman(space_id, user_id, display_name)` is built
   from the current `space_user`, as `_wake_space` does. Authority is the thread
   member's, satisfying invariants 3 and 10b.
3. **Admission.** The request is `_thread_request(store, thread, row.message,
   wake=False)` with `source` dropped. It passes through
   `_resolved_graph_request`, then `project_admission(project_id)` (the
   experiment lock plus `require_project_accepts_new_work`), then
   `admit_fresh_chat_turn`, then `background.start(...,
   operation_id=uuid5(NAMESPACE_URL, f"rcp:agent-thread-queue:{queue_id}"),
   queue_id=queue_id)`.
4. **Claim.** `create_agent_task` gains `queue_id`, next to `handoff_id` and
   `handoff_wake_id`. In the same `BEGIN IMMEDIATE` that inserts the task,
   `claim_queued_turn(connection, record, queue_id)` requires all of:
   - the row exists;
   - `refusal_reason IS NULL`;
   - `operation_id IS NULL OR operation_id = :op`;
   - an equal message, thread and member;
   - no older unclaimed, unrefused row for the thread.

   Then it stamps `operation_id`. A replayed or concurrent drain finds the
   existing task and returns it. A deleted row raises a final refusal, so a
   removed message never starts.
5. **Transient errors.** These are the `_TRANSIENT_DELIVERY` errors raised after
   the pre-check. The first one stamps `deferred_at`. After the timeout, the next
   pass refuses with `delivery_timed_out`.

`dispatch_thread_handoffs` runs the handoff pass and then
`dispatch_thread_queue`. That pass admits the oldest pending row for each
thread whose chat has no active task. Today's callers keep it running: after
each settle, on sign-in release, and at startup. The queue POST runs one pass
for its own thread through `app.state.reconcile_thread_handoffs`. This closes
the race where the turn settles between the view's last read and the insert.

## API

All routes are on `api/agent_threads.py` and are owner-only. A thread that does
not belong to the member, or a Space thread, returns 404.

- `POST /api/agent-threads/{thread_id}/queue`
  - Body `{message}`, plus an `Idempotency-Key` header.
  - Needs `require_patch_capable_identity` and the project's write admission.
  - Returns 202 `{queue_id, operation_id | null}`. `operation_id` is set when
    the POST's own pass started the turn.
  - 422: empty or too long, or a key reused with another message. 409: closed
    or archived thread, or a full queue.
- `DELETE /api/agent-threads/{thread_id}/queue/{queue_id}` deletes an unstarted
  or refused row. A started row returns 409.
- `GET /api/agent-threads/{thread_id}/queue` returns `{items}`, ordered by
  readiness:
  - `{kind: "handoff", handoff_id, message, source_agent_name, ready_at}` for
    handoffs whose source turn succeeded, with the name from `chat_display`;
  - `{kind: "message", queue_id, message, created_at, refusal_reason}`.

## Web

- `web/src/core/types.ts` gains the `AgentThreadQueueItem` union and
  `AgentThreadQueue`.
- `agentThreadsApi.ts` gains `loadThreadQueue`, `queueThreadTurn(thread,
  message, requestId)` and `removeQueuedTurn`.
- `AgentThreadView` loads the queue with the transcript on each `refresh`
  (watermark-driven) and renders the rows above the composer.
- The composer derives `queueing` from the rules in Behavior. Send then becomes
  Steer (with Queue beside it) or Queue.
- Phone width: rows truncate to one line, and buttons keep 44 px targets.

## Checks

- **Storage** (`tests/test_agent_threads_storage.py`):
  - an idempotent insert, and a key reused with another message refused;
  - one claim across two racing drains;
  - DELETE interleaved with a claim: the deleted row never starts;
  - FIFO within a thread;
  - the watermark trigger (`tests/test_resource_versions.py`).
- **Lifecycle owners:** restore deletes unstarted rows; the transfer manifest
  lists the table (`tests/test_transfer_archive_manifest.py`); member removal
  and project deletion counts.
- **Drain** (`tests/test_agent_thread_commands.py`):
  - handoffs first, then messages;
  - paused waits;
  - closed, removed member and blank name refused;
  - transient errors defer, then time out.
- **Fake-agent journey:** a running turn, then Queue, then settle; the queued
  turn starts with the member's text and identity. A removed item never starts.
- **Browser:**
  - rows render;
  - Enter steers a steerable run and queues otherwise, and queues on an idle
    thread with waiting items;
  - remove and dismiss work;
  - at 390 px.
- **Docs:** the conversations spec "Private agent threads", the API spec "Agent
  thread API", and the interface spec "Agent view".
