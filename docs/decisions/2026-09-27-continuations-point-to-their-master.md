# Continuations point to their master

## What changed

A launch that hands the provider a session id is a continuation. It sends only
what is new for that attempt, inline, and ends with one pointer to the master
contract the session started from. It never resends the master and never
forces a read. A launch with no session id is a session start and sends the
full contract, as before.

Before this change every non-chat continuation staged a file that repeated the
whole contract and told the agent to read it first. An Experiment wake file was
about 27,000 characters, of which about 1,300 were new. Episodes opened each
wake by rereading it.

## Why a pointer and not a copy

The copy existed so that a session that compacted would still have its rules.
The session already holds the contract from its start. It needs a reliable way
back to it, not a fresh copy each turn. The pointer says it is the same contract
given at the session's start, and to read it only after a compaction or when the
agent has lost track of the graph rules or its authority.

## Why the master has its own durable record

A pointer is only safe if the file exists whenever a continuation could read it.
RCP records the master's exact bytes on the operation that sent it and restores
the file into the launch's stage before every pointer. A retention sweep or a
per-turn stage cannot then leave the pointer dangling. A whole stage that is
gone still fails closed.

Only a master recorded by a succeeded operation counts. A failed, paused, or
interrupted attempt may never have delivered it, so the next launch bootstraps
again. A launch receipt is not proof of delivery. The cost is that a Resume or
Retry of a session's first attempt always bootstraps.

## Why current authority stays inline

Corrections are not one policy. Ingestion revokes operational authority, Work
keeps it, an Experiment watcher correction may repair the joint handoff, a merge
rebase replaces its context, and Auto-research correction is validate-only.
Paths, commands, credentials, write scope, skills, and each restriction are
specific to the attempt. They always travel inline and take precedence over the
master, so the pointer can never restore an expired command.

## Why the key includes owner policy

The graph rules digest alone misses owner authority, watcher rules, and recovery
policy. It also misses which rendering a project needs: the rules add extension
authoring once a project has ontology extensions. The master key joins a shared
master version, the graph rules version, one policy version per owner, and the
project's ontology mode. A key change renders and records a new master,
and the continuation says it replaces the earlier one. Paths and graph data stay
out of the key.

## Why the episode report is the exception

The report reuses the operational session but may read only its frozen inputs
and write one HTML file. It carries no pointer and says the operational
instructions no longer apply. Its restriction is then the newest instruction the
session holds, so the next operational continuation on that session reopens the
master explicitly. That requirement is durable state on the session and clears
only when an operational attempt on it succeeds.

## What a later reviewer might restore

- **A copy of the contract on every wake.** That is the redundancy this removed.
  Compaction is handled by the pointer and the durable record.
- **A registry of owner prompt profiles.** Each owner keeps its own builders and
  calls one shared `compose` in `src/rcp/agents/continuation_prompt.py`. A
  registry and a runtime tree check were reviewed and rejected as overbuilt.
- **Counting a master from an unsucceeded attempt.** It saves one bootstrap and
  risks pointing at a contract the provider never received.
