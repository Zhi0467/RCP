# A member terminal inherits the Work turn's trust boundary

Confirmed by the human 2026-09-19. This record fixes a trust decision that will
look like an oversight to a later reader, because the safer-sounding option was
deliberately rejected.

## What was decided

A project member may open an interactive shell on a registered repository, on
the server, running as the `rcp` service account. It is not isolated from that
account's authority. It is convenience for someone already trusted on the
machine, not a boundary against someone who is not.

## Why the safer option was rejected

A `gpt-6-astra` design review on 2026-09-19 showed that a shell keeping the
service identity cannot be fenced into a project. Two findings were reverified:

- The installed control socket is `/run/rcp/control.sock`
  ([`layout.py`](../../src/rcp/server_ops/layout.py)), and its handler admits any
  peer whose uid is root or the owning service uid
  ([`control.py`](../../src/rcp/server_ops/control.py)). Member removal is
  reachable there. No mount-namespace setting hides `/run`, and
  `PrivateUsers=yes` does not change the host uid.
- Canonical `.research` lives inside a registered repository, because
  `state.repository` must name one ([`config.py`](../../src/rcp/config.py)) and
  setup writes canonical state to `<that repository>/.research/`
  ([`setup.py`](../../src/rcp/setup.py)).

Real isolation therefore needs a separate execution identity, an independent
clone, and a Git credential broker — a month-scale feature.

It was rejected because it defends against a threat this product does not face,
while an equivalent door stands open beside it. A member can already run any
command as `rcp` today by asking for a Work turn: Claude's `Bash` is unbounded
and its OS sandbox is off, by
[an earlier deliberate decision](2026-09-13-claude-work-runs-without-the-os-sandbox.md),
and [`providers-and-containment.md`](../specs/providers-and-containment.md)
names that an accepted gap rather than containment. A terminal grants no
authority a member lacks; it removes the agent as a slow middleman.

Spending a month to fence the terminal while that door stays open would buy
nothing and would imply a guarantee the rest of the system does not keep.

## What this obliges

**Say it out loud.** A project member is trusted on the server that hosts their
project. That sentence belongs in the authority spec. It was previously true by
accident; it is now true on purpose.

**Do not call it containment.** The terminal may still run under a mount
namespace to survive a wrong-directory mistake, and the spec text must name that
accident resistance in the same breath, never a boundary.

**Corruption is still refused wherever the OS allows it.** Invariants 1, 2, and
6 protect against slips as much as against malice, so the terminal reuses the
protected-path construction in
[`agents/write_scope.py`](../../src/rcp/agents/write_scope.py). Whether those
paths can be made read-only is a property of the machine, not of the space or
the member: local Linux with systemd gets mirrored containment, and anything
else is cooperative and says so. On a machine that cannot enforce it, the human
already had a shell there, so the terminal takes nothing away — but it must
report the limit rather than imply the guarantee.

**Membership still gates.** Nothing here weakens the route's membership check.
A member reaches their own projects; the shell's identity is not what decides
that.

## What would reopen this

Either half of the premise failing:

- A Work turn gains real containment, at which point the terminal is the
  remaining open door and inherits a boundary it no longer has.
- A team space admits members who are not trusted on the host — an external
  collaborator, a student account, anything short of a colleague. The trade
  assumes everyone with project membership could already be handed the machine.
