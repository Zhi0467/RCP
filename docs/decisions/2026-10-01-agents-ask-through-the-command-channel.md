# Agents ask the human through the command channel

## What changed

An agent asks the human a question with an `ask` verb on the staged command
client, the channel it already uses for `validate`, `launch`, and the
orchestrator's verbs. A Work turn can wait for the answer live; the
orchestrator parks the question and ends its turn.

## Why not the providers' own ask tools

Each provider has one, but they are reachable in different ways:

- Claude Code's `AskUserQuestion` works headless only through the stdio
  permission-prompt protocol, and only outside `dontAsk`. Work launches use
  `dontAsk`, which removes the tool; allow-listing it does not bring it back.
- Codex's `request_user_input` works only through app-server, is limited to
  Plan mode, and needs a feature flag elsewhere. `codex exec` rejects it.
- OpenCode's `question` works only through `opencode serve`; `opencode run`
  always denies it.

Adopting them would mean three mechanisms and, for Claude, leaving `dontAsk`.
They stay off in every launch, so agents see exactly one way to ask.

## Why not an RCP MCP server

An `ask_human` MCP tool would work with all three providers and needs no
third-party dependency. It was rejected because it is a second agent-to-RCP
channel beside the command client, with its own authentication, records,
retry rules, and a containment exception on every provider. Claude also moves
a blocking MCP call to the background after 120 seconds unless background tasks
are disabled.

The command client already has signed requests, task events, keyed
idempotency, and `delivery` semantics, and its bounded return fits inside
provider shell-tool limits. Exposing the whole command surface through MCP
later, as a second front end over the same protocol and broker, remains
possible. That would migrate every verb at once, not add one feature.

## Why an answer is never authority

The answer is human input, stored as the human's message. A "yes" to "may I
write outside my roots?" grants nothing: capability, write scope, graph target,
and budget come from the question's origin binding and from code, never from
answer text. A change to a protected belief is still a Proposal.
