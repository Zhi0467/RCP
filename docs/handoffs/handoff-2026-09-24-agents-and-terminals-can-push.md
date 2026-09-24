# Agents and terminals can push to their repositories

Date: 2026-09-24
Status: design confirmed by the human on 2026-09-24. Implemented with
automated coverage: per-checkout `core.sshCommand`, the member identity file
for local and remote terminals and provider launches, and the missing-key
notice. The affected test files, ruff, and pre-commit pass. Remaining: the
live team-space run in the closure condition, and real SSH to a remote
execution machine, which tests exercise only by running the shipped payload
locally.

Settled by the human on 2026-09-24:

- The default push credential is the repository's deploy key, the one RCP
  already provisions for a team checkout.
- Discuss and Work turns both get it, as does the member terminal. Discuss
  keeps no repository writes, so it cannot fetch, commit, or merge. It can
  still push refs that already exist in the checkout, or delete a remote
  branch, because a push only reads the local repository. RCP cannot fence
  that in Discuss: the provider sandbox lets it read the key.
- RCP adds no per-member Git setting: no personal key, name, or email. It
  supplies a default identity that any Git configuration overrides.
- RCP membership becomes write access to the repository, because pushes
  authenticate as the deploy key. GitHub records the deploy key, not the
  member, as the pusher; only the unverified commit author names the member.
  Removing a member from the project is the revocation.

Close this handoff when, in a team space, a member's terminal and Work turn
each run `git fetch`, `git commit`, and `git push` against a registered
repository with no manual Git setup, the commit carries that member's
identity, and a Discuss turn's `git ls-remote` reaches the remote.

## The problem

Today a chat agent has no Git credential and no identity. A fetch or push
fails with `Permission denied (publickey)`. The member must push from a shell.

The terminal is only partly better. It receives the deploy key only when one
was provisioned for that repository
([`terminals.py`](../../src/rcp/api/terminals.py)); otherwise it gets nothing
and says nothing. RCP never sets a commit name or email anywhere, so every
member configures `user.name` and `user.email` by hand in each shell.

## What changes

### The key lives in each checkout's Git config

A project can register several repositories, each with its own deploy key,
and one chat turn can touch all of them. A single `GIT_SSH_COMMAND` per launch
cannot serve that. So RCP writes `core.sshCommand` into each team checkout's
local `.git/config`, pinned to that repository's deploy key and the account's
`known_hosts` with strict host checking. Every process working in the
repository, including its conversation worktrees, then uses the right key with
no launch plumbing.

`deploy_key_ssh_command` in
[`project_checkout.py`](../../src/rcp/server_ops/project_checkout.py) and
`terminal_git_access` in
[`git_access.py`](../../src/rcp/terminals/git_access.py) already build this
command. One builder owns it. Provisioning writes it after clone, and an
existing checkout gets it the next time provisioning or checkout verification
runs, so no manual step is needed on a live server. The terminal stops setting
its own `GIT_SSH_COMMAND`. Personal spaces are unchanged: Git uses the
account's own SSH setup.

### One identity file per member, used by every launch

`GIT_CONFIG_SYSTEM` points at a small generated file holding the member's
default `user.name` and `user.email`, followed by an include of
`/etc/gitconfig` when that exists so the host's system settings survive. The
terminal binds it read-only. Provider launches receive the variable through
`ProviderProcessEnvironment`
([`provider_environment.py`](../../src/rcp/agents/provider_environment.py)),
which carries a local environment and a remote prefix over SSH. The remote
prefix writes the file under the account's RCP data directory before
exporting the variable. Discuss and Work turns both receive it.

### Commit identity

The default is the member's RCP display name and
`<member-id>@members.rcp.invalid`; `.invalid` is a reserved, never-real domain,
and the member id is stable across display-name changes. A commit never fails for lack of identity. GitHub shows
the name but links it to no account.

It is written to the system config layer, Git's lowest precedence, not to
`GIT_AUTHOR_*` variables, which would override everything. A member's own
`git config` or exported variables therefore win. On a team server the global
and repository config belong to the shared service account, so a value saved
there applies to every member; an exported variable lasts one shell. RCP does
not add storage to change that. The system layer needs Git 2.32 or newer;
launch preflight refuses an older Git rather than dropping the default.

### A missing deploy key is visible

If a repository has no deploy key, a chat turn records a trace notice naming
the operator action that provisions one and still runs, so one keyless
repository cannot block every turn. A terminal open on that repository refuses
with the same message. No silent credential-less launch.

## Invariants and docs that change

- Invariant 4 in `AGENTS.md`: Discuss gains Git transport. It still gains no
  graph or project authority or repository writes, and `patch.json` stays the
  only graph channel.
- [`providers-and-containment.md`](../specs/providers-and-containment.md): the
  "Git key, provider login, and member token are independent" paragraph stays
  true, and gains the rule that every launch on a repository receives its Git
  access.
- A decision record: every team launch carries Git transport, and what that
  gives up.

## Work and checks

One pull request. The first three items are implemented; the live run remains.

- Team checkouts carry `core.sshCommand` for their deploy key, written at
  provisioning and backfilled on verification. Test: a provisioned checkout's
  config names its own key; two repositories name different keys.
- Every terminal and provider launch, local and remote, Discuss and Work, gets
  the member's identity file. Test: a PATH-shimmed `git` sees
  `GIT_CONFIG_SYSTEM` in both modes, and a repository-level `user.name`
  overrides the default.
- The missing-key notice in the terminal and chat composer.
- Live team-space run: terminal and Work each fetch, commit, and push;
  Discuss runs `git ls-remote`. This also closes the deploy-key run the terminal handoff still owes.
