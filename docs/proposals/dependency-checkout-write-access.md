# Dependency checkout write access

Status: draft design proposal, not approved or implemented.
Date: 2026-09-11.

This proposal separates permission to modify a dependency checkout from membership
in project truth scope. It does not change current launch permissions, authorize
an agent run, or supply an implementation handoff. Current behavior remains in
[Providers and containment](../specs/providers-and-containment.md) and
[the product design](../design.md).

## Problem and existing boundary

A Work turn may need to repair a dependency, install an editable package, or
write build outputs in a dependency checkout. Today `resolve_project_write_scope`
in `src/rcp/agents/write_scope.py` requires every admitted repository alias to
belong to `manifest.project.truth_scope`. Adding a checkout solely to unblock a
write therefore also changes the project's semantic boundary.

That coupling provides a useful conservative default: repository pointers and
agent instructions cannot manufacture filesystem authority. The proposal keeps
that default and separates only the human's operational grant from truth scope.
It does not replace exact containment with unrestricted shell access.

The existing continuation contract rejects changed roots before launch. Historical
commit `1466c1d` also explains why host authority comes from the execution-machine
catalog, while a pointer's host only describes how the agent reaches a checkout.
Its regression drove real staging because synthetic pointers had hidden a remote
failure. The proposed checks must use that same local/SSH resolution path.

## Proposed permission model

Keep three facts distinct:

| Fact | Meaning | Proposed owner |
| --- | --- | --- |
| Project truth scope and selected run truth scope | Which repositories participate in canonical research authority | Existing human-controlled project and run configuration |
| Dependency write grant | Which additional exact checkout roots a Work-like launch may modify | Explicit human authorization captured at task or episode admission |
| Repository access provenance | Which inputs the agent actually used and what operational work it performed | Existing Patch provenance plus a separate operational receipt |

A project member who may authorize the Work turn or episode may select an
additional dependency checkout from a project-owned registry. Registration alone
grants no write access. The proposed initial default is an empty dependency grant;
existing projects and imported records retain their present permissions.

The additional grant is available only to existing Work-like capabilities.
Discuss, ingestion, Paper coaching, and graph-only merge keep their current
capabilities. The grant does not authorize Git push, deployment, credential edits,
package installation outside the selected root, or remote source transfer.

RCP resolves one `ProjectWriteScope` containing the existing admitted roots plus
the authorized dependency roots. It does not maintain a second provider
allowlist. The launch contract and provider enforcement render the same resolved
object. The receipt distinguishes dependency roots from truth repositories so a
human can inspect the reason each root is writable.

## Resolution and containment

The dependency registry needs a stable alias, project owner, execution machine,
and exact checkout root. Reuse the existing registered-root validation instead
of accepting browser paths or trusting provider-generated pointers.

For every launch, resolve canonical paths on the actual execution host and check
the account, machine, project ownership, root existence, and writability. Retain
the complete project inventory check and fail closed on unavailable inventory.
Reject another project's checkout, a parent of several checkouts, protected RCP
state, account home, and broad temporary directories. Deduplicate exact roots;
never replace them with a shared parent. Symlink relocation must be detected by
the canonical-root comparison. A dependency on another machine is not granted
through this launch merely because an alias names it.

Adding a dependency grant never changes project truth membership, selected run
truth scope, graph target, protected belief authority, or invocation budget.
`Patch.repositories_read` is currently constrained to `run_truth_scope` by
`src/rcp/core/validation/patch.py`; it must not be repurposed as a write-permission
list or populated with every writable dependency. Record actual dependency reads
and modifications separately in operational provenance. Research claims still
need valid evidence and canonical attribution under the current graph contract.
Whether dependencies may become independently cited graph evidence is a separate
decision, not an implicit consequence of this permission.

## Session and episode transitions

Capture the human authorizer, selected grants, canonical roots, execution identity,
and resulting fingerprint at admission. Resume, Retry, correction, watcher wake,
and provider switch retain that exact authorization. Selecting a new provider
does not let a failed episode acquire newly configured dependency roots.

Recompute the scope before continuation and refuse a moved root, changed account,
missing grant, or incompatible fingerprint. Do not silently widen or shrink an
existing native session. A changed dependency selection requires a fresh ordinary
Work session, or a new human-authorized episode after the existing episode stops
and its live calls settle. Preserve its history and report; do not present the
new episode as the old invocation's Retry.

The proposed first implementation adds no mid-session grant transition and no new
exception to conversation worktree integration. Existing records without grants
decode as empty. A future implementation must define the request compatibility
and persisted fingerprint version before shipping a new stored field.

## Acceptance scenarios for implementation tests

These scenarios describe required tests; this documentation PR does not execute
provider writes or claim an implementation pass. Use disposable checkout roots
and the real resolver, staging, and fake-provider launch path, including SSH.

1. Given truth repository `core` and registered dependency `helper`, launching
   without a grant denies writes to `helper`. Launching fresh with explicit human
   authorization permits a file edit only inside its exact root; truth scope and
   the Patch authority remain unchanged.
2. Given an authorized dependency on the execution host, stage its real pointer
   through the ordinary pipeline. Local and SSH launches expose the same resolved
   roots in prompts and provider enforcement; pointer transport metadata cannot
   select another host.
3. Given a symlink to another project's checkout, a broad parent, or an unavailable
   project inventory, admission fails before any provider starts. No wider parent
   root or prompt-only fallback appears.
4. Given a session-limit failure, Retry and provider switch retain the original
   dependency grants, stage binding, and invocation count. Adding a project grant
   while the episode is failed does not expand either continuation.
5. Given a relocated dependency or revoked grant, continuation refuses before
   launch. An explicitly authorized fresh session or episode may resolve the new
   selection after the old call settles; the old record remains unchanged.
6. Given an actual read of `helper`, operational provenance records that access.
   A dependency grant alone cannot add `helper` to `Patch.repositories_read` or
   change project truth membership. An ungranted write still fails in the provider.

## Open decisions before implementation

- Confirm project-level registration with per-admission selection, or choose a
  different persistence scope. Per-admission selection is the proposed default;
  silently granting every registered dependency is excluded.
- Decide whether a dependency must use a dedicated project-owned checkout or may
  be shared outside another project's ownership. The proposed first scope allows
  only a dedicated checkout; cross-project grants remain excluded.
- Define the human revocation flow. The proposed rule fences future launches and
  requires settling a live call before narrowing its grant; it does not claim to
  revoke an already-running provider's filesystem access retroactively.
- Choose the operational provenance representation and whether dependency reads
  need any future canonical evidence extension. Do not weaken existing Patch
  validation as a shortcut.

If approved, implement the registry/admission field, single-scope resolver,
continuation compatibility, and permission UI together with the acceptance tests.
Update current specs only in that implementation change after these decisions
are resolved. This draft remains separate from runtime recovery and release work.
