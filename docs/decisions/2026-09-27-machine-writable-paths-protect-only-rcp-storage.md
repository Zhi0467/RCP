# Machine writable paths protect only RCP's own storage

Confirmed by the human 2026-09-27.

## What happened

A user on the team server could not fill a shared Hugging Face cache from any
RCP surface. Every write scope was built from registered repositories only, so
the terminal, Codex Work, and systemd compute jobs could not write anywhere
else, and Codex Work could not even write `/tmp`: RCP's own permission profile
had left it out. The only way out was a shell outside RCP.

## The decision

- Each space keeps machine cards, and each card lists writable paths. Every
  launch that already writes repositories receives them, plus `/tmp` and the
  local temporary directory. Launches without repository write roots receive
  none.
- RCP constrains grants only to protect itself. Home folders, `/tmp`, `~/.ssh`,
  and other projects' repositories can all be granted. RCP's storage (the data
  directory, installed server folders, `~/.rcp`, remote RCP credentials, every
  `.research`, the launch's inputs, and legacy stages) stays read-only inside a
  grant, and a grant inside it is refused.
- RCP keeps none of its own files in `/tmp`, so `/tmp` holds nothing to protect.

## How this reads invariant 4

Invariant 4 says configuration cannot widen agent capability. Which launches may
write, and which must stay scratch-only or read-only, stays fixed in code. A
human chooses the roots, as registered repositories already did. Discuss,
ingestion, paper coach, and graph-only branch merges never receive grants.

## What this costs

Claude's deny rules beat its allow rules, so a protected folder that holds the
launch's own stage is left undenied for Claude. With a grant covering that
folder, Claude's file tools can edit sibling stages there. This amends the
2026-09-13 decision, which kept Claude's file tools bounded: inside such a grant
they are bounded only as far as deny rules can express. Claude's shell was
already unbounded. The human chose this over an RCP-owned file-edit hook.

## Claude's shell stays unwrapped

Claude's own sandbox cannot run scheduler work: a team-host probe on
2026-09-27 (Claude Code 2.1.283) showed it cuts the network namespace even with
no domain list, so `sinfo` and `srun` fail. Wrapping Claude in an RCP-owned
systemd unit was reviewed and rejected by the human the same day: it touches the
command broker's ancestry check, Stop and crash recovery, and environment
handoff, and Docker or Slurm could still write outside it. Claude's shell is
bounded by the prompt's write boundary (`write_scope_section`) and agent
behavior, not by the OS. Do not re-propose a separate OS account for agents.
