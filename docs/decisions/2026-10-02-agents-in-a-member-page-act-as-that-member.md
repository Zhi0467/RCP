# Agents in a member's page act as that member

Date: 2026-10-02. Status: active. Its live voice checks are among
[the open live checks](../handoffs/README.md).

## Decision

An agent the member runs inside their own authenticated RCP page acts as that
member. Today that means a WebMCP host agent; next, RCP's voice agent. Its tool
calls are the member's actions, and history records the member. There is no
agent actor.

It may do what the shared tool list allows: read, open views, send Discuss and
Work messages, start an Experiment, authorize Auto-research with any budget,
and gracefully stop an episode. Protected judgment stays tap-only and outside
the list: Proposal judgment, Decision choice, node standing, Hypothesis status,
truth membership, and branch-merge dispatch.

The voice agent adds a confirmation toggle the member controls. By default a
Work message, Experiment Start, or Auto-research authorization waits for a tap
on a card that pins the exact action. WebMCP host agents get no RCP
confirmation; any confirmation is the host agent's own.

## What this reinterprets

- **Invariant 3** ("only humans authorize episodes") now reads: the member's
  own authenticated session authorizes, whatever the input device or the agent
  the member chose to run in it. Nothing changes for provider agents, which
  never run in a member's page.
- **The four boundaries** (authority spec): "a prompt or model can never widen
  another boundary" still holds. The agent's capability is the tool catalog,
  fixed in web code (boundaries 2 and 3). The model only chooses among those
  tools. The confirmation toggle removes a guard; it adds no capability, so
  invariant 4 holds.
- **Auto-research carries the orchestrator's Decision exception.** Authorizing
  Auto-research by voice or WebMCP hands the orchestrator Decision choice on its
  own branch, as a tap does today. The agent in the page never chooses a
  Decision itself.
- **`docs/design.md`** says WebMCP adds no human-judgment authority. Starting
  episodes and sending Work were already product actions the member's page
  could take; that sentence gains Auto-research authorization and voice.
- **Voice may run a terminal command as the member.** It can type one command
  line into a fresh project terminal and read back the output; it never types
  into a shell that was already open. Every command waits
  for a tap on a card that shows the exact command and repository, even when
  voice confirmation is off. WebMCP does not get this tool, because a host
  agent has no RCP card to show.
- **Invariant 10d** is about prior transcripts becoming task authority. The
  voice agent can read a conversation and then send a Work message the member
  asked for. That message is new input from the member's session, recorded as
  the member's, not a replayed transcript.

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

- **A misheard phrase or another voice can start paid work** when voice
  confirmation is off.
- **Prompt injection.** The agent reads project content that other agents and
  people wrote: node text, chat answers, artifact listings. Injected text could
  steer it into a Work message or an Auto-research authorization with any
  budget. For voice, tap mode is the guard, and it is the default. For WebMCP,
  RCP has no guard; only the host agent's own confirmation stands between
  injected text and paid work.
- **A confirmed command has the member's full terminal power.** The tap checks
  the exact line, but the shell does whatever that line does.
- **History cannot tell an agent's action from a click.** A voice or WebMCP
  action records only the member.
- **The member owns the bill.** Voice runs on the member's own key, and
  Auto-research authorization spends project budget the member could also spend
  by hand.
