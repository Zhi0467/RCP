# Main is the convention-governed server-update channel until public sharing

**Status:** accepted on 2026-08-27; enforcement timing revised by the human on
2026-08-28.

## Decision

The source-built team server updates only from GitHub `origin/main`, so every
commit on `main` must be deployable. RCP development moves from direct work on
`main` to short-lived feature or WIP branches merged through pull requests.

During the private one-lab development phase, this workflow is convention-only.
The current lint, Python, Web, and upgrade-compatibility CI jobs must pass, the
branch must be current with `main` or have its combined result tested, and merge
is an explicit human action. Coding agents may create and update branches and
pull requests, but they neither push directly to `main` nor merge without direct
human instruction. No second reviewer account is required.

GitHub does not currently enforce that policy. A maintainer with write authority
can still push directly or merge with a failed or missing check. G1 must state
that limitation plainly and verify the PR/CI workflow without claiming that a
forbidden action was technically rejected.

Before RCP is shared publicly with people outside the current private one-lab
development setting, the repository becomes public and `main` receives real
branch protection. That public-sharing gate must require pull requests and the
named build, test, and upgrade-compatibility checks, reject direct pushes and
failed or missing checks, and record a live enforcement proof. Public protection
is a settled later requirement, but it does not block completion of the current
private team-server slice.

Local Web and desktop development may run any branch. Draft pull requests may
remain unfinished and contain arbitrary intermediate commits; the server never
consumes them. Emergency fixes use the same short-lived-branch, green-CI, and
explicit-human-merge policy.

RCP will not add a permanent `dev` integration branch. Such a branch would
create a second long-lived release line, accumulate combinations that differ
from production, and merely move the question of deployability from `main` to
promotion time.

After the current CI baseline is green and before server implementation begins,
G1 updates `AGENTS.md`, contributor guidance, and stable CI job names together.
Until G1 lands, the existing direct-`main` repository instruction remains current
behavior rather than a silent half-migration.

## Why

A read-only check on 2026-08-28 confirmed that `Zhi0467/RCP` is private and its
current GitHub plan rejects branch-protection configuration with HTTP 403.
Protected private `main` would therefore require a paid-plan or ownership change.
The human chose to keep the present private development setup convention-only
and defer technical enforcement until the repository is intentionally made
public for wider sharing.

This accepts a known interim risk: CI can report a bad direct push or merge, but
cannot prevent it. Short-lived pull requests, green checks, and explicit human
merge still keep the normal development path reviewable without introducing a
second release branch or forcing a repository visibility/billing change now.

## Rejected alternatives

- Upgrade or transfer the private repository solely to obtain protection now:
  adds a billing or ownership change the human does not want for this phase.
- Make the repository public before RCP is ready to share: changes visibility
  earlier than intended.
- Treat direct pushes as the normal workflow: allows an unreviewed commit to
  become the server update target and contradicts the accepted PR policy.
- Add a permanent `dev` branch: creates another drifting release line and a
  separate promotion event.
- Let the server select arbitrary feature branches: turns development state into
  production configuration and defeats one trusted update channel.
- Require a second reviewer account for every pull request: makes a one-maintainer
  repository unable to ship without adding the accepted safety boundary.
