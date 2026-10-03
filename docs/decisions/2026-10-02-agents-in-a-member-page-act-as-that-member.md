# Agents in a member's page act as that member

Date: 2026-10-02. Status: active. The work is planned in the
[voice agent handoff](../handoffs/handoff-2026-10-02-voice-agent.md).

## Decision

An agent the member runs inside their own authenticated RCP page acts as that
member. Today that means a WebMCP host agent; next, RCP's voice agent. Its tool
calls are the member's actions, and history records the member. There is no
agent actor.

It may do what the shared tool list allows: read, open views, send Discuss and
Work messages, start an Experiment, authorize Auto-research, and gracefully
stop an episode. Protected judgment stays tap-only and outside the list:
Proposal judgment, Decision choice, node standing, Hypothesis status, truth
membership, and branch-merge dispatch.

The voice agent adds a confirmation toggle the member controls. By default a
Work message, Experiment Start, or Auto-research authorization waits for a tap.

This reads invariant 3's "only humans authorize episodes" as: the member's own
authenticated session authorizes, whatever the input device or the agent the
member chose to run in it. It changes nothing for provider agents, which never
run in a member's page.

## Why

- **The precedent exists.** WebMCP Experiment Start already authorizes an
  episode through the member's session, attributed to the member.
- **The member is present and asking.** A spoken or typed request in the
  member's own page is the member's intent. The agent gets no capability the
  page lacks.
- **A distinct actor buys nothing here.** It would need a new principal type
  across authentication, records, and membership checks, and its authority
  would still come from the member who started it.
- **Usefulness.** A voice agent that can only read cannot follow up on what
  it finds.

## What this gives up

- **A misheard phrase or another voice can start paid work** when confirmation
  is off.
- **Prompt injection.** The agent reads project content that other agents and
  people wrote: node text, chat answers, artifacts. With confirmation off,
  injected text could steer it into a Work message or an Auto-research
  authorization. Tap mode is the guard, and it is the default.
- **History cannot tell an agent's action from a click.** A voice or WebMCP
  action records only the member.
- **The member owns the bill.** Voice runs on the member's own key, and
  Auto-research authorization spends project budget the member could also spend
  by hand.
