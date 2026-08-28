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
wrapper, and systemd service are installable. Raw bootstrap, member, session,
provider, and Git credentials are absent; only nonsecret hashes/identifiers
needed to prove credential survival remain. Paths in every database and manifest
are relative to the fixture root, and the test always operates on a temporary
copy.
