# Main is the direct development and server-update channel until wider sharing

**Status:** accepted on 2026-08-27; private-development workflow superseded by
the human on 2026-08-28.

## Decision

The source-built team server updates only from GitHub `origin/main`, so every
commit consumed by a lab server must be deployable. Throughout the current
private, single-developer implementation of the first team-server slice, work
continues directly on local `main`; this handoff does not introduce a feature-
branch, pull-request, or human-merge gate.

Before recording or pushing a scoped change, the developer runs the checks
appropriate to that slice, including focused tests, pre-commit, and a code review
for coverage, edge cases, and stale documentation. Full source-built desktop and
live machine drives occur at meaningful integration milestones rather than after
every file-sized packet. CI still reports lint, Python, Web, and upgrade results
on pushed `main`, but GitHub does not prevent a bad direct push. That limitation
is explicit rather than described as enforcement.

From the first team-server-capable commit onward, every server-era persistence
boundary remains directly upgradeable. The old-data job and immutable fixtures
remain required evidence even though they are not a pull-request gate.

Before RCP is shared publicly or with external users, make the repository public
and enable real `main` branch protection. That later gate requires pull requests
and the named build, test, and upgrade-compatibility checks, rejects direct
pushes and failed or missing checks, and retains an explicit human merge. Record
a live enforcement proof then. This public-sharing transition is outside the
current one-lab implementation goal.

The same transition retires the private-source deploy key. A public origin is
readable without a grant, and going public means no supported private origin, so
the `grant_needed` install pause, the `source_ed25519` key material, and the
`rcp-source:<installation-id>` label that backup records for later revocation
all become dead and are removed together. Per-project deploy keys are unrelated
and stay: those are write grants on each team project's own repository. Until
that transition, keep the README's deploy-key step conditional on a private
origin rather than describing it as an unconditional install step.

There is no permanent `dev` branch, and a server never consumes an arbitrary
feature branch. Emergency changes during this private phase follow the same
direct-`main`, scoped-verification discipline.

## Why

The current phase has one human developer and no established lab server yet.
The human chose to keep the full pre-team-server implementation on `main`
instead of paying the coordination cost of a convention-only pull-request flow
that the private repository cannot enforce and that adds no independent reviewer.
Once `main` becomes a real server update channel, deployability and historical
upgrade evidence still matter; the simplified development path does not weaken
those product guarantees.

A read-only check on 2026-08-28 also confirmed that `Zhi0467/RCP` is private and
its current GitHub plan rejects branch-protection configuration with HTTP 403.
Public sharing is the chosen point to change visibility and add technical
enforcement rather than doing so during this private build-out.

## Rejected alternatives

- Require short-lived PR branches during the current implementation: adds a
  convention-only ceremony without another reviewer or technical enforcement;
  the human explicitly rejected it for this phase.
- Upgrade or transfer the private repository solely to obtain protection now:
  adds a billing or ownership change the human does not want for this phase.
- Make the repository public before RCP is ready to share: changes visibility
  earlier than intended.
- Add a permanent `dev` branch: creates another drifting release line and a
  separate promotion event.
- Let the server select arbitrary feature branches: turns development state into
  production configuration and defeats one trusted update channel.
