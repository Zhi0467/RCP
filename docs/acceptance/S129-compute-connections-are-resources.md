---
id: S129-compute-connections-are-resources
status: pending
tier: remote
driver: pytest + browser + ssh
covered_by:
  - tests/test_api_project_state.py
  - tests/test_compute_connections.py
  - tests/test_config.py
  - tests/test_api.py
  - tests/test_transport_containment.py
  - tests/test_chat_prompt_protocol.py
  - tests/test_prompts.py
  - web/tests/compute.test.mjs
  - web/tests/projectSession.test.mjs
  - web/tests/providerReadiness.test.mjs
  - web/tests/settingsDraft.test.mjs
invariants: [4, 5, 10d]
last_checked: >-
  2026-09-03 — full backend and Web suites passed; disposable browser
  verification covered Settings save/reload,
  local and unavailable probe presentation, composer attachment recovery,
  native checkbox pointer/keyboard operation, desktop and 390 px layouts,
  unchanged run_on, and no console errors; a
  disposable acceptance-agent Work launch confirmed exact selected metadata,
  an unchanged local execution binding, and secret-free provider context, while
  regressions cover initial Discuss/Work context, resumed add/remove/update/no-op
  deltas, immutable admission and watcher snapshots, delayed-launch manifest
  changes, redacted diagnostics, bounded schemas, stale-status masking, and
  in-flight readiness invalidation. Live authentication/host-key targets remain
  pending.
---

# Attach compute without moving the agent

Compute connections are project resources available to an already-running
conversation. They never select the provider execution host, expand authority,
or collect credentials.

## Setup

A project with two agent execution machines, one local compute connection, and
one SSH compute target that is reachable from one execution account. Prepare a
second SSH target without credentials and one with a deliberately untrusted host
key. Use disposable accounts and project data.

## Drive

1. Open Project Settings. Add local and SSH connections with names and non-secret
   access hints. Confirm there is no password or private-key input, save, reload,
   and inspect the manifest and API response for metadata only.
2. Probe readiness. Confirm each connection has a separate result for each agent
   execution machine. Confirm success is green and unavailable is red, with
   unreachable, authentication, and host-key failures distinguished.
3. Read the authentication and host-key actions. Confirm each names the exact
   agent execution machine where ordinary SSH state must be repaired, and that
   strict host-key checking remains enabled.
4. Open a project conversation, attach both computes through the **Compute**
   menu, and send one turn. Inspect the durable task request, provider launch
   host, canonical chat record, master/task context, and provider answer.
5. Send an ordinary second turn without changing compute, then detach one and
   send a third. Confirm the provider did not move or relaunch, `run_on` and write
   scope did not change, the unchanged turn did not repeat compute context, and
   the changed turn carried only a concise delta. An addition or update includes
   the bounded non-secret profile needed to use it; a removal includes only its
   display name.
6. Reload after each turn and confirm the menu restores the newest active set.
   Remove one configured connection in Settings and confirm the stale attachment
   disappears without affecting the conversation's execution profile.

## Pass condition

Connection metadata and active selection survive reload; pairwise readiness is
truthful; the agent receives only selected non-secret access metadata; no
credential enters RCP; and provider execution identity, `run_on`, graph authority,
repository scope, and write scope remain unchanged.
