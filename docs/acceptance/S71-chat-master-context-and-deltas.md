---
id: S71-chat-master-context-and-deltas
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_chat_prompt_protocol.py::test_fresh_discuss_bootstraps_one_master_with_both_mode_contracts
  - tests/test_chat_prompt_protocol.py::test_ordinary_resumed_discuss_sends_only_marker_message_without_unchanged_context
  - tests/test_chat_prompt_protocol.py::test_mode_switch_resumes_same_native_session_and_appends_only_changed_settings
  - tests/test_chat_prompt_protocol.py::test_node_chat_master_carries_the_focused_node_and_its_relations
  - tests/test_chat_prompt_protocol.py::test_a_human_sync_between_turns_announces_only_the_new_revision
  - tests/test_chat_prompt_protocol.py::test_a_work_turn_does_not_announce_its_own_revision_back_to_itself
last_passed: 2026-08-05
invariants: [4, 4b, 10, 10b, 10c, 10d]
---

# A chat sends one master context, then only turn markers and deltas

The first launch of a native chat session receives one master context. It names
the stable graph, research, repository, schema, skill, workflow, and output
pointers and contains both the Discuss and Work contracts. The master protocol
states that exactly one contract is active per turn; merely seeing the inactive
contract grants no authority.

A node chat's master also carries the focused node and its one-hop relations
inline, so the first turn can answer without a lookup. That snapshot is stated
as of the bootstrap revision and is never refreshed: RCP does not track the
focused node's content across turns, and the agent re-reads the graph when the
node's current wording matters.

Every later ordinary turn resumes the same provider session. Its prompt contains
the human's original message unchanged plus exactly one explicit Discuss or Work
turn marker and the logical turn id used by the master's artifact-path template.
It does not resend the master contract or unchanged context.

When Settings or a stable pointer changed since the prior turn, RCP appends one
compact context-update block naming only the changed fields and their new values.
Those replacements become the session's current context for following turns.
Unchanged fields are omitted. Slash invocation tokens remain in the human text;
enabled-package changes and pointer changes travel through the same delta.

The graph revision travels through that same delta, and it is the one signal
that says the graph moved. It must mean *moved by someone else* — a human Sync
between turns — so a Work turn that applied its own patch does not announce its
own revision back to itself on the following turn.

## Assert

- A fresh chat launch sends and records one master context containing both mode
  contracts and an unambiguous active-turn selection rule.
- A normal resumed Discuss or Work turn sends the mode marker and byte-for-byte
  human message without a task-contract pointer or repeated graph/repository
  context.
- Switching mode keeps the native provider session while the launcher applies
  the selected turn's Discuss or Work CLI capability.
- A changed contract version, repository pointer, Settings-enabled package set,
  package version/path, graph revision, or other stable pointer produces a
  compact delta. An unchanged context produces no delta.
- A fresh node chat's master context contains the focused node's fields and its
  one-hop relations, stated as of a named revision.
- A human Sync between two turns produces a revision delta on the next turn. A
  Work turn whose own patch applied produces none for that patch.
- The captured RCP mode remains authoritative for graph handling: Discuss has
  no Patch channel; Work may submit one semantic `patch.json`; final validation
  and append remain RCP-owned.
- Resume of an interrupted attempt and bounded correction remain explicit
  continuation messages; they do not masquerade as a new human turn.
