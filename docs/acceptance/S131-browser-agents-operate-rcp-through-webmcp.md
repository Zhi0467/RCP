---
id: S131-browser-agents-operate-rcp-through-webmcp
status: implemented
tier: hermetic
driver: browser
covered_by:
  - web/tests/webmcp.test.mjs
  - web/tests/chatWorkspace.test.mjs
  - web/tests/resultViews.test.mjs
invariants: [3, 4, 4b, 10, 10b, 10e, 10g]
last_passed: >-
  2026-09-03 — a disposable acceptance-agent served-browser drive verified
  index-to-project registration, exact reads, asynchronous Work dispatch and
  later conversation inspection, dynamic Experiment Start/Stop availability,
  graceful Stop, and a backend task artifact opening in RCP's page viewer. The
  complete 589-test Web suite and production build also passed. The drive had no
  page exception or failed application request; Chromium emitted only an
  unsupported navigate-to CSP directive warning while the sandboxed viewer
  rendered normally.
---

# Let a browser agent operate RCP without creating a second authority plane

A compatible browser agent can orient itself in RCP, inspect saved research and
results, continue an ordinary provider conversation, and start or gracefully
stop an already-defined bounded Experiment. The agent uses the same current
project projections and actions as the visible application.

## Drive

1. Open the ready project index in a WebMCP-capable browser. Confirm only
   `rcp_list_projects` and `rcp_open_project` are present, list the current cards,
   and open one exact returned project id.
2. Confirm the index tools retire after navigation and the project tools appear.
   Read the compact overview, inspect an exact node, list artifacts, and open one
   available artifact or episode report in RCP's page viewer.
3. Inspect an existing conversation. Confirm its latest task, bounded messages,
   provider/session configuration, available skills, and any refusal are current.
   Send one Discuss or Work message and confirm the call returns its durable task
   and conversation ids without waiting for provider completion.
4. Inspect a ready Experiment, start its next bounded episode, and confirm the
   Start tool remains stable until the accepted call returns. Inspect the new
   exact episode, request graceful Stop with both returned ids, and confirm an
   unavailable Start or Stop is no longer advertised.
5. Navigate to login or project setup and confirm the project tools are removed.

## Pass condition

Every tool result is bounded and machine-readable; stale ids and unavailable
actions fail before dispatch; navigation and visual opening happen in the
current RCP page; and mutations use the existing authenticated API owners. No
WebMCP call can judge a Proposal, choose a Decision, edit or Sync the graph,
change settings or membership, or turn provider output into canonical truth.

## Boundary

WebMCP does not add a backend endpoint, provider, or session store. Browsers
without the host API receive the ordinary RCP application with no
registration attempt. Conversation tasks remain asynchronous, remote execution
continues through the saved provider profile, and human-only product judgments
remain on their existing visible controls.
