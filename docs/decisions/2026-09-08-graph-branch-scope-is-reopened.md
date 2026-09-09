# Graph-branch scope is reopened toward a version-control model

Confirmed by the human 2026-09-08. This record retires an absolute exclusion. It
does not claim any of the admitted work ships, and it is not a backlog.

## What changed

The earlier boundary read as a permanent product judgment: no branch manager, no
conflict editor, no cherry-pick, no discard, no repository-branch control, no
automatic merge. That phrasing foreclosed a direction the codebase has been
converging on anyway. Graph branches, an append-only Patch log, a three-way
semantic delta, deterministic conflict detection and a merge receipt already
describe a version-control system for the research graph.

The exclusion is retired. A version-control model for the research graph is an
admitted direction: inspecting a branch, switching between branches, committing a
Patch, and merging branches one at a time into main.

## What stays fixed

- **Human authority is not part of the metaphor.** Git has no notion of protected
  content. RCP does. Protected beliefs still route through Proposals, only humans
  approve them, and only humans dispatch a merge
  ([invariants 3 and 3b](../../AGENTS.md)). A branch surface that lets agents
  merge protected changes on their own is out of scope, permanently.
- **Merges stay pairwise and sequential.** Each merge is one branch against
  current main, exactly as a person merges branches one at a time. No operation
  merges a set of branches at once.
- **Agentless is not automatic.** A merge whose non-conflicting residue is empty
  may commit without a provider turn. It still requires human dispatch. "No
  automatic merge" means no merge without a human, and that remains true.
- **Graph branches are still not Git branches.** They version the research graph.
  Repository branches, repository rollback and worktree selection stay in the
  [conversation contract](../specs/conversations-episodes-and-watchers.md).

## Why the deterministic merge core comes first

The merge validator already computes the answer it grades against. `mandatory`
is the set of paths the branch changed and main did not, and success requires the
merged graph to equal the branch value at each one. That is a pointwise
specification, held by RCP before the agent starts, yet nothing builds a Patch
from it — the only producer is the provider.

So the first piece of work is a deterministic builder: emit the ordinary
non-conflicting operations directly, route protected changes to Proposals, and
hand the agent only the conflict residue. An empty residue commits with no
provider turn. This is the prerequisite for everything above, because a branch
model people use many times a day cannot spend a paid multi-minute agent turn on
a merge whose outcome is already determined.

The [`crlp` merge deadlock](../specs/auto-research-and-branch-merge.md) that
prompted this record is a separate defect, fixed alongside it. A deterministic
builder would not have avoided it on its own; RCP-authored operations pass
through the same validation.
