# Team launches carry Git transport, including Discuss

Confirmed by the human 2026-09-24.

## What was decided

Every process working in a team checkout, including a Discuss turn, carries
that repository's deploy key. Terminals and Work fetch, commit, and push with
it. RCP supplies a default commit
identity and stores no per-member Git key, name, or email.

## What this gives up

- **Membership is write access.** GitHub sees the deploy key, not a member, as
  the pusher. A member with no GitHub access to the repository can still push.
  Removing them from the project is the revocation.
- **Authorship is unverified.** The commit author is whatever Git config says.
  Anyone can claim any name unless the repository requires signed commits.
- **Discuss is no longer transport-free.** It can reach the remote. Its
  repository writes stay at none, confirmed by the human, so it cannot fetch,
  commit, or push; letting it write `.git` was rejected as a wider Discuss.

## Why

Before this, a chat agent failed every fetch and push with
`Permission denied (publickey)`, and every terminal needed a hand-set name and
email. The human chose convenience over per-member attribution, and rejected
personal keys because a key stored under the shared service account is readable
by every member's shell and agent anyway
([terminal trust decision](2026-09-19-a-member-terminal-inherits-the-work-trust-boundary.md)).

## Why the key lives in the checkout

A project registers several repositories with separate deploy keys, and one
turn can touch all of them. A launch-wide `GIT_SSH_COMMAND` can name only one
key; `core.sshCommand` in each checkout names the right one for every process.
