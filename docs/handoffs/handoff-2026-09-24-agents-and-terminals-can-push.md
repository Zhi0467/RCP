# Agents and terminals can push to their repositories

Date: 2026-09-24
Status: design proposed, not yet confirmed. Nothing is implemented.

Settled by the human on 2026-09-24:

- The default push credential is the repository's deploy key, the one RCP
  already provisions for a team checkout.
- Discuss and Work turns both get it, as does the member terminal.
- A member may optionally add their own key once. When present, it replaces
  the deploy key for that member's terminal and chat turns.

Open for confirmation: the commit identity rule below.

Close this handoff when, in a team space, a member's terminal, Discuss turn,
and Work turn each run `git fetch`, `git commit`, and `git push` against a
registered repository with no manual Git setup, and the commit carries that
member's identity.

## The problem

Today a chat agent has no Git credential and no identity. A fetch or push
fails with `Permission denied (publickey)`. The member must push from a shell.

The terminal is only partly better. It receives the deploy key only when one
was provisioned for that repository
([`terminals.py`](../../src/rcp/api/terminals.py)); otherwise it gets nothing
and says nothing. RCP never sets a commit name or email anywhere, so every
member configures `user.name` and `user.email` by hand in each shell.

## What changes

### One Git access object, used by every launch

`terminal_git_access` in
[`git_access.py`](../../src/rcp/terminals/git_access.py) already builds the
right `GIT_SSH_COMMAND` for a deploy key. It becomes the single owner of "Git
access for this member on this repository", returning:

- the key to use: the member's own key if they added one, else the deploy key;
- `GIT_SSH_COMMAND` pinned to that key and the account's `known_hosts`;
- `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, and
  `GIT_COMMITTER_EMAIL` for the member.

The terminal keeps calling it. Provider launches start calling it through
`ProviderProcessEnvironment`
([`provider_environment.py`](../../src/rcp/agents/provider_environment.py)),
which already carries a per-process environment locally and a remote prefix
over SSH. No second implementation.

### Commit identity (to confirm)

- Default: the member's RCP display name, with a placeholder email derived
  from their member id. A commit never fails for lack of identity. GitHub shows
  the name but links it to no account.
- Optional: the member enters a Git name and email once, next to their own
  key. Commits then link to their GitHub account.

### A member's own key

Stored beside the deploy keys under `rcp-server/credentials`, one file per
member, mode 0600, owned by the service account. It is added and removed from
the member's own settings, never shown back. The terminal decision record
already states the honest limit: anything the service account can read, a
member shell or agent can read. A personal key stored here is readable by
other members' agents and shells on the same server. The settings screen must
say so in one sentence.

### A missing deploy key is visible

If a repository has no deploy key and the member has no own key, the
terminal and the chat composer say so, and name the operator action that
provisions one. No silent credential-less launch.

## Invariants and docs that change

- Invariant 4 in `AGENTS.md`: Discuss gains Git transport. It still gains no
  graph or project authority, and `patch.json` stays the only graph channel.
- [`providers-and-containment.md`](../specs/providers-and-containment.md): the
  "Git key, provider login, and member token are independent" paragraph stays
  true, and gains the rule that every launch on a repository receives its Git
  access.
- A decision record: agents may push, including from Discuss, and why the
  separation of Discuss from repository writes was dropped.

## Slices

1. Git access owner: own key, deploy-key fallback, identity environment.
   Terminal uses it. Tests: environment per case, missing-key refusal.
2. Provider launches receive it, local and remote, for Discuss and Work.
   Test: a PATH-shimmed `git` sees the variables in both modes.
3. Member settings: add or remove own key and identity; missing-key notice.
4. Live team-space run: terminal, Discuss, and Work each fetch, commit, and
   push. This also closes the deploy-key run the terminal handoff still owes.
