---
id: S75-network-access-on-every-agent-surface
status: implemented
tier: live
driver: pytest + browser
covered_by:
  - tests/test_launcher.py::test_codex_resume_keeps_the_write_permission_its_surface_was_given
  - tests/test_launcher.py::test_codex_new_read_only_session_has_no_workspace_write_config
  - tests/test_launcher.py::test_claude_scratch_patch_preauthorizes_only_native_web_retrieval
  - tests/test_launcher.py::test_claude_read_only_command_keeps_plan_permission_mode
  - tests/test_launcher.py::test_claude_discuss_keeps_scratch_writable_without_auto_mode
  - tests/test_api.py::test_paper_coach_uses_agent_task_manager_and_result_shape
  - tests/test_prompts.py::test_graph_contract_keeps_fanout_and_points_to_payload_files
  - tests/test_prompts.py::test_paper_and_continuation_contracts_only_point_to_dynamic_content
requires: an authenticated Claude CLI for the live Claude Node Chat drive
invariants: [3, 4, 10]
reported_by: human, 2026-08-07
last_passed: 2026-08-12 — authenticated Claude 2.1.227 read identifiable public
  pages from Node Discuss, a second turn in the same native session, Project
  Chat, Work, Refresh, and Paper Coach; every launch receipt recorded network
  access with the surface's unchanged capability, the paper stayed byte-exact,
  and the clean browser tab had no warnings or errors
---

# Every user-facing agent task can read the public web

An agent may need current external evidence whether it is seeding the graph,
answering in Discuss, doing Work, or coaching the paper. Choosing a read-only
surface limits what the agent may change; it does not make the public web
unavailable.

Network access means the provider can use its native web search and fetch tools
without an interactive approval that RCP cannot present. It does not grant
additional shell commands, repository write roots, graph authority, or paper
authorship.

## UI path (confirmed)

Confirmed by the human on 2026-08-07: no new setting or permission control is
added. From Node Chat, Project Chat, Runs, and the Paper workspace, the existing
agent actions can consult a public URL. The mode toggle continues to control
Discuss versus Work authority only; it does not toggle network access.

Internal generic Patch-correction continuations remain offline. They are not
independent human tasks and may only repair the retained `patch.json` from
already-staged inputs.

## Drive

1. Configure an authenticated provider and open a project.
2. In a node's chat, leave the conversation in Discuss and ask the agent to read
   a public page whose contents are identifiable from the reply.
3. Repeat from Project Chat, a Work turn, Seed or Refresh, and Paper Coach.
4. Exercise the provider launch contract for both Claude and Codex, including a
   resumed native session.

The Codex Node Chat drive passed on 2026-08-07 against the live workshop page.
The full Claude drive passed on 2026-08-12 against `example.com` and the public
catastrophic-interference page. The live drive caught two truthful contract
gaps before passing: Paper Coach and Seed/Refresh had the provider grant but did
not say so in their closed staged contracts, so Claude correctly refused. Their
contracts now grant only read-only native web search and fetch for relevant
public evidence and still forbid external side effects. One pre-reset Refresh
attempt hit Claude's stated session limit; the clean post-reset run fetched the
page and completed normally.

## Assert

- `node_discuss_reads_the_requested_page` — the reply uses the page's actual
  content and does not report that web search or fetch is ungranted.
- `every_user_facing_capability_has_web_access` — Discuss, Work, Seed/Refresh,
  and Paper Coach launch with usable native web search and fetch access.
- `resume_keeps_web_access` — continuing a native session does not lose the
  grant.
- `authority_is_unchanged` — Discuss and Paper Coach remain read-only,
  Seed/Refresh remain scratch-only, and only Work retains exact admitted-project
  operational write access plus its optional semantic graph channel.
- `receipts_tell_the_truth` — every user-facing task records network access as
  enabled; offline Patch correction remains explicit.

## Failure means

An agent claims that web tools are unavailable, a launch receipt claims access
the provider was not granted, or enabling web research silently widens file,
repository, graph, or paper-writing authority.
