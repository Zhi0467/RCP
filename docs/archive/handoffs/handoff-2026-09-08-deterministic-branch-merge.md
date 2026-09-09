# Deterministic branch merge

Date: 2026-09-08
Status: completed locally on PR #102. The builder and scope correction are
implemented; publishing this follow-up and PR CI remain separate release steps.
Current behavior is owned by the
[branch merge specification](../../specs/auto-research-and-branch-merge.md).

## Delivered

`build_deterministic_merge_ops` uses the validator's semantic paths to build
ordinary non-conflicting node creations, updates, and legal edge additions and
removals. It preserves compatible main-side values and skips already-delivered
values. Empty residue takes the existing validation and commit path with no
provider call.

For mixed merges, RCP prepends the fixed operations during self-check and final
validation. The agent writes only the remaining operations. Conflicting nodes
and Decision choices stay intact so coupled fields can be updated atomically.
Protected relations use the existing authority classifier and remain subject
to Proposal review. Invalid fixed operations, unsupported configuration changes,
and mandatory source Proposals outside current membership fail before launch.

Merge dispatch pins current project truth membership, including repositories
outside the ordinary default run selection. A later membership change requires
a new human dispatch. Repository write scope remains empty.

The existing conformance gate, human authorization, correction/rebase limits,
atomic append, and receipt reconciliation remain in place. There is no new
patch kind, registry, conflict heuristic, branch UI, or shared execution policy.

## Verification

Real HistoryManager regressions cover ordinary sourced creates, updates,
relations, nested field preservation, protected residue, atomic Decision choices,
correction, a moving main, and failure without a provider call. Merge API tests
cover non-default provenance, pending source Proposals, excluded repositories,
membership drift, watcher delivery, and paused-episode retirement.

A disposable served-browser drive merged branch-only Evidence into one main
revision without a provider call. The episode showed MERGED and project history
showed both the branch and merge tasks on main. Console warnings/errors were
empty. Remote/provider qualification was not run for this follow-up.
