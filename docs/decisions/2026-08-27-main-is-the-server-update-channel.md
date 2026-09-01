# Main is the server-update channel; development switches after stabilization

**Status:** accepted on 2026-08-27; amended by the human on 2026-09-01 to set the
end of the direct-development exception.

## Decision

The source-built team server updates only from GitHub `origin/main`, so every
commit consumed by a lab server must be deployable. The still-open first-team-
server stabilization and closure slice continues directly on local `main`,
including compatibility work required to make that server stable. This is a
bounded exception: it ends when the active dev-team-space-and-server handoff
meets its exact closure condition and is archived.

Before recording or pushing a scoped change, the developer runs the checks
appropriate to that slice, including focused tests, pre-commit, and a code review
for coverage, edge cases, and stale documentation. Full source-built desktop and
live machine drives occur at meaningful integration milestones rather than after
every file-sized packet. CI still reports lint, Python, Web, and upgrade results
on pushed `main`, but GitHub does not prevent a bad direct push. That limitation
is explicit rather than described as enforcement.

From the next change after that closure onward, development uses a short-lived
branch, a pull request with CI, and an explicit human merge. This applies even if
the repository is still private and GitHub cannot technically enforce the rule.
The server never consumes a development branch; it sees only the merged commit
on `origin/main`. There is no permanent `dev` branch.

From the first team-server-capable commit onward, every server-era persistence
boundary remains directly upgradeable. The old-data job and immutable fixtures
remain required evidence throughout both workflows.

Before RCP is shared publicly or with external users, make the repository public
and enable real `main` branch protection. That later change technically requires
the already-adopted pull-request workflow and its named build, test, and upgrade-
compatibility checks, rejects direct pushes and failed or missing checks, and
records a live enforcement proof. Public sharing changes enforcement, not the
workflow transition date.

The same transition retires the private-source deploy key. A public origin is
readable without a grant, and going public means no supported private origin, so
the `grant_needed` install pause, the `source_ed25519` key material, and the
`rcp-source:<installation-id>` label that backup records for later revocation
all become dead and are removed together. Per-project deploy keys are unrelated
and stay: those are write grants on each team project's own repository. Until
that transition, keep the README's deploy-key step conditional on a private
origin rather than describing it as an unconditional install step.

Emergency changes during the open stabilization slice follow its direct-`main`,
scoped-verification discipline. After closure, emergency changes also use a
short-lived pull request unless the human explicitly authorizes an exceptional
process for that incident.

## Why

The first implementation began with one human developer and no established lab
server. The human chose to keep that bounded build-out on `main` rather than
change process before the server path existed. A real lab server now exists but
the same closure slice is still repairing and qualifying its compatibility and
end-to-end operation, so changing workflow in the middle of that unstable slice
would create a second moving boundary. Its explicit handoff closure is the
observable point at which that reason expires.

After closure, CI-before-merge and human review are useful even without technical
branch protection because `main` is a live deployment channel. Deployability and
historical upgrade evidence continue unchanged on both sides of the transition.

A read-only check on 2026-08-28 also confirmed that `Zhi0467/RCP` is private and
its current GitHub plan rejects branch-protection configuration with HTTP 403.
Public sharing remains the chosen point to change visibility and add technical
enforcement, but no longer the point at which the team adopts pull requests.

## Rejected alternatives

- Switch to pull requests before the current stabilization handoff closes:
  changes development process while the first live server is still being
  repaired and qualified; the human retained direct commits for this slice.
- Keep direct pushes until public sharing: leaves a live server update channel
  without CI-before-merge or an explicit human merge after stabilization.
- Upgrade or transfer the private repository solely to obtain protection now:
  adds a billing or ownership change the human does not want for this phase.
- Make the repository public before RCP is ready to share: changes visibility
  earlier than intended.
- Add a permanent `dev` branch: creates another drifting release line and a
  separate promotion event.
- Let the server select arbitrary feature branches: turns development state into
  production configuration and defeats one trusted update channel.
