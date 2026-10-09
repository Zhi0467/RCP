# Server upgrade fixtures

Each child directory is an immutable, sanitized copy of one real server-era
persistence boundary. The external registry in `tests/server_upgrade_harness.py`
pins the exact directory set, source commit, and whole-bundle digest; each bundle
also inventories its payload files. SQLite is stored as deterministic gzip only
to keep immutable historical bytes under the repository's per-file size limit;
the candidate expands a copy before opening it through current migrations. It
then starts the complete backend with the acceptance agent and checks health,
canonical replay and Patch immutability, startup recovery, credentials/session
rows, membership, SQLite integrity, and public project/task projections.

Do not regenerate an existing boundary with newer RCP code. A persistence change
adds a new sibling created while the old shape is still current. Removing a
boundary requires a separately approved migration retirement; fixture age alone
is never a reason. Never open a fixture database in place: copy the whole
boundary first, because SQLite may create WAL/SHM sidecars even for inspection.

The first bundle is created by the first team-server code. Each later bundle is
a copy of its predecessor opened and settled by that boundary's exact source,
so the sequence retains tables and migration effects a fresh database would
miss. During the episode-vocabulary era the legacy Experiment task is completed;
the next and subsequent pre-repair starts then produce the known contradictory
legacy wrap-up. Its metadata names that expected repair, and the candidate test
proves the row exists before current migration removes it.

The registry starts with `team-server-v1-78be62b`, the merge that first made the
team backend runnable, and retains the later episode-vocabulary,
orchestrated-child, graph-target, provider-runtime, and modern Experiment-repair
boundaries. `source-server-install-v7-638c19e` is the first boundary at which the
root-coordinated source installer, managed checkout/release layout, stable
wrapper, and systemd service are installable.
`project-provisioning-v8-227f964` adds one in-progress team-project preparation
and its idempotent step receipt while proving that the proposed project has no
catalog or canonical authority. Raw bootstrap, member, session, provider, and
Git credentials are absent; only nonsecret hashes/identifiers needed to prove
credential survival remain. Project and manifest paths are relative to the
fixture root; the provisioning row additionally carries the product's fixed
server central root. The test always operates on a temporary copy.
`central-checkout-v9-a499be3` retains that migrated pre-configuration request and
adds one in-progress request with P4's complete project configuration and
nullable schema boundary. It proves a later candidate can still read the old
row while retaining the new manifest inputs needed for final creation.
`update-cutover-v10-db3173b` records the last database shape before terminal
tasks gain a durable history-only fence. It includes the source-update cutover,
backup, and rollback-era migrations that the next candidate must preserve while
adding that marker.
`pre-member-removal-v11-27c9682` records the resulting last database shape before
team members gain durable removal fences and tombstones, and before team and
project invitations gain explicit revocation state.
`pre-storage-migration-ledger-v12-c3191bf` records the last shape before the
first expensive semantic migrations gained durable completion markers.
`storage-ledger-v13-371b807` records that first ledger era before startup schema
completion and validation moved under one ordered migration plan.
`pre-compute-jobs-v14-7d6546f` records the v0.3.5 release shape: the last before
compute jobs, external watcher actions, child Work watcher routes, and compute
job labels added their tables and columns. It is the boundary an installed
v0.3.5 server upgrades from.

`pre-artifacts-v15-ff090e1` retains the final inline report and legacy result-view
storage shape before artifact bytes move into the data directory. It is built
by the exact boundary source from the preceding immutable fixture.

`pre-consolidation-v16-61f6fc1` retains the last shape before operational
lessons and nightly graph consolidation added their tables (storage migrations
36 and 37). It also carries the question, chat projection, and artifact policy
migrations that followed the previous boundary. It is built by the exact
boundary source from the preceding immutable fixture.

`pre-provider-autocompact-v17-dfb553b` retains the last shape before machine
cards gained a per-provider auto-compact map (storage migration 42). It also
carries the owner sign-in, browser grant, and machine hidden-folder migrations
that followed the previous boundary. It is built by the exact boundary source
from the preceding immutable fixture.

`pre-per-target-loops-v18-3640725` retains the last shape before Experiment
loops gained per-graph-target live identity (storage migration 44). It also
carries the per-provider auto-compact and shell-timeout migrations, ending at
migration 43. It is built by the exact boundary source from the preceding
immutable fixture.

`pre-client-requests-v19-92c9de7` retains the last shape before voice writes
gained durable client request ids (storage migration 45). It also carries the
per-graph-target Experiment loop migration, ending at migration 44. It is built
by the exact boundary source from the preceding immutable fixture.
