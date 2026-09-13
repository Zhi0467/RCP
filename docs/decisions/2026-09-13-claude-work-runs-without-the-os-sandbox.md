# Claude Work runs without the OS sandbox

Confirmed by the human 2026-09-13. This record fixes a containment tradeoff that
is easy to regress, because it already regressed once silently.

## What happened

Claude Work and orchestrate launches used to carry no OS sandbox. A provider
settings block added on 2026-08-19 turned Claude's own sandbox on, fail-closed,
with `allowUnsandboxedCommands` disabled.

On Linux that sandbox is bubblewrap, and three of its restrictions are not
configurable:

- `--unshare-net` whenever `network.allowedDomains` is defined, which it always
  effectively is. Egress then exists only through an HTTP/SOCKS proxy, so every
  non-HTTP protocol is unreachable.
- `--dev /dev` unconditionally, so no accelerator device node is visible.
- `--unshare-pid` unconditionally.

A seccomp filter also denies the `AF_UNIX` socket family outright, unless
`network.allowAllUnixSockets` is set.

A Work turn under that sandbox therefore cannot reach a scheduler, an
accelerator, its own RCP command broker, or any non-HTTP service on the host it
was launched on. Scheduler clients need both a Unix socket for local
authentication and a real network path to the controller, and lose both.

## Why it went unnoticed for three weeks

The regression landed during a gap in which no Claude Work turn ran. When they
resumed, most failed for unrelated reasons — usage and session limits, a broken
transport — and the turns that completed only edited files. The first turn asked
to submit a scheduler job after the change reported it as an environment blocker
rather than a provider regression.

Before the change, every scheduler watcher created by a Claude Work turn was a
real queue watcher. After it, the only one is a file-existence watcher waiting
for a human to submit the job. That contrast is the evidence, not an inference
from the sandbox's documented behavior.

## The decision

Claude Work and orchestrate launches set `sandbox.enabled: false`. Work on this
provider exists to run real compute, and containment that forbids reaching a
scheduler or an accelerator is not usable containment for that purpose.

Exact write roots stay enforced, through Claude's own file permission rules.
`Edit(path)` is the only rule kind matched for file writes and it covers every
file-editing tool; a `Write(path)` rule is accepted and then ignored, so RCP
emits only the `Edit` form. The previous settings emitted both, meaning half of
that allow and deny list never did anything even while the sandbox was on.

## What this costs, stated plainly

`Bash` is not bounded by permission rules. A Claude Work turn's shell can write
outside its admitted roots on its execution machine. This is an accepted
accidental-write gap for one provider, not a claim of containment. Codex's native
permission profile still bounds both its file tools and its shell, and its
profile keeps network access, which is why Codex was never affected.

Nothing here widens graph authority. `patch.json` in the task stage remains the
only graph-change channel ([invariant 4b](../../AGENTS.md)), protected beliefs
still route through Proposals, and no configuration can widen agent capability
([invariant 4](../../AGENTS.md)).

## What would change this

A sandbox that can bound filesystem writes without also unsharing the network
namespace and the device tree would let Work keep both properties. Until one
exists, re-enabling the sandbox for this provider silently removes the ability to
run compute, and must not be done as a cleanup.
