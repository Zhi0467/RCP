# Server and machine operations

Installed server, machine authority, deployment, checkouts, provisioning, transfer
and backup contracts. [Projects and spaces](projects-spaces-and-operations.md)
owns identity/membership; [server.md](../server.md) owns operator procedures.

## Agent secret hiding and repository authentication

The backend owns one long-lived deploy-key `ssh-agent` per OS account, guarded
by an account lock even when backends use different data directories. Its private
stable socket directory lives under `~/.rcp`, outside `/tmp`, so the service's
`PrivateTmp` does not split the transport. Startup opens the effect fence, starts
the agent, and then admits recovery launches; shutdown drains workers before
stopping it. The user's `SSH_AUTH_SOCK` remains separate. Provisioning, restore,
and rotation load deploy keys immediately after writing them when the local
account agent is running; the next launch can hide them without a restart.

Repository `core.sshCommand` retains its transport and adds the stable
`IdentityAgent` socket. A deploy key is hidden only after fingerprint identity
and bounded signing confirmation on the launch host. Unconfirmed keys remain
readable so Git keeps working. On remote execution machines, the existing generic
launch helper starts one long-lived account agent through a systemd user unit
on Linux or launchd on macOS, on the first launch that needs it, using
`~/.rcp/ssh-agent/agent.sock`. A staged stdlib helper loads keys from `~/.local/share/rcp/credentials` and confirms deploy
keys against that stable socket and user identities against the remote account's
own `SSH_AUTH_SOCK` on each launch. Remote `core.sshCommand` keeps `-i` and adds
`IdentityAgent` when the account agent runs. Missing agents or failed identity or
signing checks leave the affected keys readable for that turn.

Settings and doctor show missing hiding support, blocked Linux user namespaces,
readable private keys, and the macOS browser exception. There is no per-turn
warning trace or badge; the agent prompt retains its effective status. Local
Settings checks account key evidence as well as wrapper readiness; remote
Settings leaves status unchecked until launch. Launches with these gaps report
unhidden; hiding availability never blocks provider launches or shrinks browser
and Git capability. This is selected-secret protection against generic network prompt
injection, not a separate account or a hostile same-account sandbox.

## External dependencies

`src/rcp/dependencies.py` owns program presence, roles/platforms and required versus
optional status; [AGENTS.md](../../AGENTS.md#cross-cutting-implementation-rules)
owns registration rules. Machine cards, install and doctor use the same registry.
Missing required tools refuse runs; optional gaps disable their named feature.
`tests/test_external_dependencies.py` checks launch sites, scripts and the apt line.

`dependency_check.py` checks local macOS/Linux in-process and remote Linux through
`staged_dependency_check.sh` over state SSH transport, accepting only nonce-bounded
output. Other remote OSes are unsupported; untested Linux distributions get a note.
At `admit_provider_task`, only definite missing/unsupported refuses before task
creation. Timeout, unreachable host or unreadable output is `not checked` and
admits. Exit 127 means missing `sh`. Dropped connections use transport backoff.
Definite results cache ten minutes, unchecked one; missing is rechecked before refusal.

Feature/version contracts stay with owners: Git worktrees 2.38, browser Node 20,
bwrap/Seatbelt, systemd-run 254 expansion and age 1.x. Remote helpers use Python
3.9 standard library only.

`transport/state_transfer.py` selects rsync by actual flags and protocol >=29,
not release number; GNU rsync and macOS openrsync qualify. It caches completed
verdicts per host/process and the absolute local path, preserving PATH order.
Exit 127 or protocol mismatch reprobes. SSH 255, timeout or spawn errors are
uncached unavailable-host outcomes, not warned contract failures.
Only `_sync_remote_tree`, `_publish`, `_publish_committed_history` and
`RemoteRunStage.finalize_inputs` use engine selection. Backup/restore, kept files,
views and source indexing require rsync. All owners retry dropped streams through
`state_transfer.run_rsync`.

Tar fallback validates archive/extraction before applying to the existing mirror;
input pushes include only listed paths. Pulls replace files via temporary siblings,
create directories/links, then delete absent entries except exclusions. Root stays
in place; interruption leaves complete old/new files and retry converges.
First fallback per host logs a warning and active-task event with failed end,
version and required installation. Project readiness exposes `state_transfers`
engine/version after transfer without probing for display. Fallback never blocks work.

## Remote display state

Remote display reads pin immutable local generations for the whole operation.
Background capture holds remote `.refresh.lock`, downloads privately, validates
and atomically publishes a descriptor. Cold reads report unavailable and queue
capture; they never wait for transport or initialize state. Retired generations
remain until readers exit. Failure retains the last copy and reports sync error/time
separately from content versions.

The lifespan coordinator unions membership-filtered open sets under independent
60-second leases and captures each remote workspace every ten seconds. Tab
activation bypasses cadence and coalesces one follow-up. Shutdown drains workers.
Changed/deleted paths advance graph, branch, chat, Paper and Settings tokens;
unchanged captures update health only, which graph tokens fold in as
reachability. Local state uses direct reads/counters.

Capture and authoritative publication share sequencing. Writes retain canonical
transactions. Retry/Resume/chat preparation pins display inputs and resolves
branches without initialization; execution reloads authoritative config for scope
checks. Pause needs no graph. Stop commits its SQLite fence before rebuilding
display controls. Retry/Resume/chat idempotency receipts commit with admission.

## Confirmed first team-server target

The supported deployment is one lab, one Linux server, one team space, and
macOS desktop clients. Ubuntu 22.04/24.04 LTS on x86-64 with systemd are tested;
other distributions and architectures are unverified. The server runs a verified
promoted wheel, hashed dependency lock and prebuilt Web bundle without reload.
[Release selection](#release-selection-deployment-and-automatic-recovery) owns
installation and recovery; [the operator guide](../server.md) owns commands.

The dedicated `rcp` account has home `/home/rcp`, shell `/bin/bash`, no usable
password, general sudo, or privileged supplementary group. Its unusable shadow
value is `*NP*`, not an OpenSSH-blocking leading `!`. Installation does not edit
`sshd_config` or enable password SSH. Direct key-only access requires an explicit
operator grant; a named operator with narrow sudo is preferred for independent
revocation and audit. RCP does not choose that account or edit sudo policy.

Git, OpenSSH, system-wide `uv`, and upstream age 1.x are prerequisites. Install
creates the account, then installs and validates its uv-managed Python 3.12.
It enables linger and requires `Linger=yes`; doctor diagnoses missing linger.
CI builds Web assets with Node.js 24; servers need Node/npm only for optional
features such as the agent browser. Root install/update best-effort installs the
fixed Ubuntu Chromium library set (t64 on 24.04), without adding apt repositories.
Failure is a warning. Browser readiness names the needed apt command, rendered
for the viewer's own shell by `operator_commands`; browser installation itself
never elevates.

Service-owned releases, data, central checkouts, keys and checkpoint payloads
live under `/home/rcp/rcp-server/`. Root owns `/etc/rcp/supervisor/`, its runtime,
selected receipt, operation/adoption journals, restore receipts and bounded logs.
Other integration paths are `/etc/rcp/server.toml`, `/etc/rcp/current`, the
root-only backup identity and public sidecar, `/run/rcp/control.sock`, CLI
wrappers, systemd units and journald. Backup destination is configurable.

Provider-native state stays in the execution account's normal home; SSH state
stays in `.ssh`. RCP's Claude setup token lives in
`providers/claude/<account>/setup-token` under app data (0700 directory, 0600
file), outside protected backups. Any member can sign in, verify and sign out
Codex/Claude through the product; dead logins refuse new work. OpenCode login
remains operator-managed. See [provider authentication](../server.md#11-provider-authentication).

Provider update runs the native updater as `rcp`, never reads credentials, and
verifies the selected executable and login separately. Service PATH puts
`/home/rcp/.local/bin` first; discovery then checks declared native locations,
including `~/.opencode/bin`, and saves stable command paths rather than resolved
versioned symlinks. The Codex installer runs noninteractively without launching
Codex or removing an older package-manager installation.

The immutable random `installation_id` is distinct from `space_id` and member
identity. New installs need no source deploy key; restore still recognizes old
`rcp-source:<installation-id>` grant labels. New backups do not emit them.
The service binds loopback. Desktop SSH transport, RCP membership, repository
deploy keys, provider login and execution-account ownership remain separate.
Remote checkouts belong to their explicitly configured SSH execution account;
member checkouts remain independent. Public HTTPS, containers, Linux desktop
packages, hosted service and multi-server failover are outside this target.

### Release check and update notices

`release_check.py` polls GitHub stable shortly after startup and every six hours.
For a newer release it verifies a published same-commit `desktop-vX.Y.Z`
companion with zip and checksum. Routes reuse checks younger than ten minutes;
Settings refresh uses one minute. Concurrent readers take cached status rather
than wait. Failures record check time and a bounded reason, never response bodies;
success clears the error. Team status uses the selected receipt, reports pins,
and never lets lookup failure alter doctor's install-integrity `source_state`.
Doctor performs its own live lookup. `RCP_UPDATE_CHECK=off` disables all calls.
Requests carry no credentials, project data or installation id.

Source `rcp serve` holds a shared `<checkout>/.rcp-serve.lock`.
`scripts/update-from-source` needs its exclusive lock before checkout/build and
restores the starting revision and build on failure.

## Machine authority and the operator surface

RCP has no administrator member role. Installation, deployment, backup/restore,
credential provisioning and removing another human require machine authority.
Member tokens cannot perform them. `rcp server ...` owns these operations, not
ordinary graph, chat, task or membership actions.

Root coordinates `install`, `backup configure`, `restore`, `update`, supervisor
update and provider update. It drops to `rcp` for application/data/provider work.
Privileged retained commands use a separate root-owned runtime pinned to the
selected build; root never executes the service-owned app environment.
`doctor`, `provider check`, `project provision`, `project transfer-import`,
`backup run` and `member remove` run as `rcp`, directly or through narrow sudo.
Wrong identity refuses before durable work; root's home never supplies credentials.

**Why:** the service account cannot create itself or replace its own system
service. Separating root coordination from service-owned installation avoids
production files and credentials depending on an operator's checkout or home.

Paired-wheel bootstrap verifies one promoted bundle and converges the account,
paths, prerequisites, backup identity and supervisor. It fetches no source tree.
Application preparation sets umask 077 and restores it on exit, validates modes
and ownership, and reuses sealed builds only with matching origin identity.
Interrupted preparations retain exact paths and diagnostics. Supervisor/operator
Python and runtimes are root-owned, separate from service Python.

Fresh install leaves the unit stopped and disabled until interactive
`space init --team` as `rcp`. The wizard runs that action on Enter, requires a
second confirmation that the bootstrap code was saved, then re-enters install,
starts and reads back service. Closing or quitting leaves the exact continue
command usable. Existing initialized spaces converge to running. Initialization
restricts SQLite to owner-only before exposing the code; resumed install refuses
wider modes. The installed wrapper resolves `RCP_DATA_DIR`.

Commands use the private `rcp`-owned control socket while the service owns data.
Offline install/restore must prove service stop and acquire the instance lock.
No member HTTP route exposes machine authority. The versioned probe advertises
operations; doctor requires both provider-plan and provider-check. Provider check
resolves one request/project, shows its plan and returns bounded results.
The socket retains provisioning, transfer, snapshots and member removal, not a
deployment coordinator. Maintenance is root-authenticated; compute probing needs
protocol 12's explicit route, while other operations retain versions 8–11.
Older clients are not advertised compute probe support.

Interactive output keeps one current-step line. Human stops name the target,
ordered actions, nonsecret values and exact continue command; Enter executes
those declared actions, while `q`, EOF or disconnect pauses. A pause can change
its task-facing title/purpose, not step identity. Optional shell context remains
separate from target authority. Redirected output stays bounded and readable;
`--machine-readable` is the full append-only event stream and never prompts or
runs human actions. Neither renderer emits secrets.

Failures report bounded causes, safe-state fields, recovery and exact diagnostic
reruns. Candidate failure also names retained capture/result and safe cleanup
paths. Socket refusals contribute operator text only after peer, request,
instance and protocol authentication; success-only fields are not required.
Fenced probe errors retain a redacted cause and private log reference; failed
recovery reports both deployment and recovery causes.

Member removal previews work, memberships, credentials and invitations. It
refuses the last enrolled member and any sole project member; pending invitations
do not count, and operators cannot assign membership to bypass the guard.
One transaction fences access, revokes tokens/sessions and unconsumed authored
space invitations, revokes authored/received project invitations distinctly from
declines, and removes memberships. The user row/name remains a tombstone.
Graceful owners settle live work; Apply rechecks membership. Service connections
are deleted under their writer lock after fencing and before completion.
Startup/re-entry resumes removal until no live work remains.

Self-revocation likewise cannot strand the last enrolled member or a project's
only authenticating member. Atomic rotation returns a replacement before
invalidating the old token. Operators cannot mint or impersonate credentials;
another enrolled member handles loss through invitation.

### Compute backend probe

`rcp server compute probe --project <project_id> <machine_alias> --route <scheduler|helper>`
runs inside the service through its control socket as `rcp`. It stores a
`ComputeBackendProbe` and returns `ServerControlComputeProbeResult` with service,
project, machine and route identities, readiness, containment and repair action.
Exit 0 means ready; otherwise 1. Helper probing checks separation from the
service. Slurm probing checks tools and queue access through the execution
account's bounded login shell; it submits no job or scheduler changes. Actual
submission validates resources and permissions. This is the only compute CLI verb.

### Package identity and offline storage migration

`rcp --version` reports verbatim version, build and commit without serving.
Source builds say `build none commit none`; base version precedes `+`.
`--machine-readable` emits the existing event shape with `version`,
`base_version`, `build`, `commit`; absent values are strings `none`, unlike
health's JSON nulls.

`rcp migrate [--data-dir PATH] [--machine-readable]` and `--check` resolve data
like `serve` and first acquire its exclusive instance lock. Contention refuses
without mutation; neither migrates beside a server or silently uses a copy.
`--check` opens SQLite read-only, including committed WAL (normal sidecars may
change), and reports ledger head, registry head and ordered pending entries:

- `fresh`: no database; no file is created.
- `current`: complete ledger and passing validation.
- `pending`: known ordered migrations, including recognized frozen pre-ledger
  boundaries, without applying them.

Those states exit 0. Unknown/ahead ledger, failed validation, unrecognized
nonempty pre-ledger data or invalid SQLite exits 2 without database mutation.
Actual migrate uses the same classification, applies known migrations through
normal store opening, validates, reports the resulting head and exits 0 without
serving. Machine-readable output ends with the exit outcome.

Install converges; there is no uninstall verb. Teardown is:

```bash
sudo systemctl disable --now rcp.service rcp-backup.timer
sudo rm -f /etc/systemd/system/rcp.service \
  /etc/systemd/system/rcp-backup.service /etc/systemd/system/rcp-backup.timer
sudo systemctl daemon-reload
sudo rm -rf /etc/rcp /usr/local/bin/rcp /usr/local/bin/rcp-supervisor
sudo userdel -r rcp
```

Skip unconfigured backup units. `/run/rcp` disappears with service stop.
Before deleting `/etc/rcp`, copy its recovery identity to protected off-host
storage or accept permanently undecryptable backups. Deleting the account removes
team data, checkouts and keys; GitHub grants still need revocation there.

## Keeping a Mac awake

In personal macOS spaces, `machine_power.py` prevents idle sleep while work needs
it. Settings → This Mac has an enabled-by-default toggle; preference is in SQLite
`machine_power_state`, not manifests. Team API returns 404; other platforms report
unsupported. `PLATFORM_PROFILES` declares support and lid capability;
`machine_power_macos.py` owns platform operations.

Demand runs `caffeinate -i -w <backend pid>` without root or lasting state. Locking
or display sleep does not end it; lid closure, battery or thermal emergencies can.
`machine_sleep.py` holds automatic launches behind a 30-second stable-wake gate.
Every ten seconds demand includes locally owned starting/active/recovering/stopping/
wrapping-up episodes, admitted queued/running/pausing tasks, nonidle runtime or
scheduled transport retry. Sign-in-blocked wrapping-up/tasks, human-only waits,
ended episodes and armed watchers alone do not count. Failed reads retain the hold
and log; they are not evidence work ended.

### Lid-closed mode

Opt-in lid mode sets `SleepDisabled` during demand. **Why:** idle assertions do
not prevent lid sleep; an independent watchdog restores sleep if the backend dies.
One admin prompt installs visudo-validated `/etc/sudoers.d/rcp-keep-awake`, granting
only `/usr/bin/pmset -a disablesleep 0` and `1`; a root boot-reset LaunchDaemon;
and `/Library/Application Support/RCP/keep-awake/`. Foreign files or a second
account refuse. Uninstall clears/verifies the flag and removes all but directory
and `owner.lock`. Install/uninstall reload the reset daemon.

One advisory machine-wide lock owns the flag. A set flag without an activation
record is `external_owner`, never adopted/cleared by the controller. Only detached
`machine_power_watchdog.sh` invokes disablesleep. Ten-second heartbeats bind
generation, PID/start time and desired state. A fresh on-heartbeat for a live
process keeps the flag; otherwise the watchdog clears/readbacks and invokes
`pmset sleepnow` unless lid is known open. Stale/mismatched generations are revoked
before exit so a resumed backend cannot re-arm them.

While enabled or cleaning up, each safety pass reads battery, thermal, lid and
flag through ctypes macOS frameworks: IOKit, `NSProcessInfo.thermalState`,
`AppleClamshellState`, `IOPMCopySystemPowerSettings`. Missing data is failure,
with no command fallback. Battery <=20% off AC, thermal serious/critical, read
failure, incomplete install or watchdog failure releases lid mode; idle hold stays
demand-driven. Demand ending drops both without sleeping an open Mac. The shell
watchdog separately parses pmset/ioreg; only it treats omitted `SleepDisabled`
under a valid settings header as off.

Thermal/cleanup failures latch off across restart until human re-enable; uninstall
clears the latch. Battery release re-arms on AC. Failed clear keeps its cause and
waits for human action, not repeated watchdogs. Home shows
`sudo pmset -a disablesleep 0` if clearing failed, or `pmset sleepnow` if a closed
Mac did not sleep after clearing. Removing sudoers can strand the flag. Preferences
and latches share `machine_power_state`.

## Release selection, deployment, and automatic recovery

**Why:** an independent supervisor can recover even when the new application
cannot start. CI promotion preserves tested bytes; rebuilding on each server
would not. The application owns admission and semantic proof, while the
supervisor owns release selection, journals and filesystem rollback.

The separately versioned `supervisor/` distribution imports no RCP modules and
has no runtime dependencies. Its low-level `fetch`, `verify`, and unprivileged
`install` commands prepare bundles. Installed `rcp server install`, `update`,
`restore`, and `supervisor update` delegate to it using the existing versioned
operator event envelope. `--plan` is side-effect-free. Non-deployment operations retain their application owners.

Installed config schema 3 has `[release] followed = "stable"` and an optional
explicit `pin = "vX.Y.Z"`. The team address lives separately in
`/etc/rcp/team.toml` (`access_url = "https://…"`, same root:rcp 0640 rules),
so the strict schema-3 file never grows a field a pinned older release would
reject; it names the one https origin members' own devices open, is
operator-set and read-only to members through the team API, and doctor reports
it as `team_access_url`, naming the file as a problem when it is unreadable or
invalid. Stable is the newest non-prerelease GitHub Release;
prereleases, missing assets, unsupported selectors, altered hashes, and
inconsistent wheel/manifest identities refuse without choosing another source.
The release metadata binds the full commit to the wheel's short commit suffix.
A root-owned selected receipt binds tag, complete wheel version, build, full
commit, manifest SHA-256, release directory, and bundled supervisor version.
The root-owned current pointer must agree with it before application launch.

`server update` displays an exact `vX.Y.Z:manifest-sha256` target for operator
confirmation. Preparation uses a new isolated service-owned release directory;
no failed preparation overwrites an existing directory. A newer required
supervisor version must be installed first through `server supervisor update`;
that refusal's next actions name it before the rerun. That command validates a separate root-owned runtime and atomically switches its
pointer under the same operation lock, without rolling back application data.

The target's `update-rehearsal` child command takes an online SQLite snapshot
and byte-copies local project roots under its output directory. It runs the
target's full inspection, preparation, and application-proof checks on that copy,
without the outgoing release's receipt, and returns `ready` with project warnings.
The service keeps running; rehearsal takes no live instance lock and never changes
admission. The caller removes the output directory. This checks
the captured copy, not a later update boundary.

### Application boundary and local checkpoint

Deployment attempts a protected backup before closing admission. An incomplete
or unreadable backup becomes `backup_warning`, not an update refusal. Root
authenticates maintenance RPC against service PID, account, instance and data
identity. The application closes mutations, launches, watchers, machine work
and recovery, drains entered work (up to two hours for agent turns), and returns
a SQLite capture bound to that quiet boundary. A drain timeout refuses cleanly.
The drain never waits on a human answer: closed admission parks every waiting
`ask` ([why](conversations-episodes-and-watchers.md#conversation-scratch-and-human-input)).

The candidate inventories local roots from captured SQLite. It rechecks failed
backup inventories; unreadable projects become named warnings and are excluded
from both copy proof and live proof. Identity, ownership, checkout/path safety,
migration and proof failures on proven projects still refuse. Before stop,
`check-space` estimates allocated bytes/inodes plus margin on each destination
filesystem. Copy errors decide success; there is no fixed size, entry or elapsed
copy ceiling. Cancellation terminates and reaps owned children.

After proving the service PID is gone, root snapshots the whole data directory
and every registered local canonical `.research` root. Nested roots collapse.
The snapshot must cover all replacement roots declared by candidate preparation.
It includes credentials, scratch, unknown files, empty directories and special
nodes without typed filtering. Provider homes, source checkouts and remote
canonical roots are outside replacement scope. The candidate proof never owns
rollback authority.

**Why:** backup's typed inventory can lag behind valid data written by the app.
A stopped whole-root snapshot still restores that data exactly. Keeping backup
warnings separate lets the core service update without hiding lost proof coverage.
Stored-data readers enforce their safety needs, not stricter copies of writer
rules; secret redaction is not a stored-data validity rule.

Before selection, effects are fenced and only local state changes. Remote live
checks compare a current local display cache or leave the project unopened;
validation already replayed its captured main and branch heads on copies and
refuses any head that differs. Remote projects
open only after selection, when rollback is no longer possible. Artifact versions
are in the data root; legacy kept files retain typed checks until imported.

Offline proof exercises API projections, main/branch replay, merge receipts,
retained tasks/stages, imported history and startup recovery. `migrate --check`
alone is insufficient. After rollback the old binary runs its own fenced proof,
without parsing a newer proof schema. The supervisor handles opaque snapshots
and proof digests.

`upgrade_graph_projection` verifies the baseline digest, removes retired
`coverage`, and fills absent deterministic defaults using the real nested graph
models. It preserves explicit values, unknown keys, stored edge layers and
serializer-owned operations. Only constant defaults, built-in empty factories
and model factories composed of them are safe; other factories need explicit
migration. Display caches receive the same upgrade, or replay if behind the
proof. Recursive tests cover every reachable model, populated collections and
optional fields, default omission, idempotence and preservation; missing examples
or unproven factories fail by field name.

Persistence migrations retain frozen, sanitized fixtures for every server-era
schema/semantics boundary and exercise the exact candidate base. Fixtures are
created with the code that wrote that boundary, never regenerated by new code.
**Why:** operators may skip updates; current models cannot prove what old code
stored. Removing a supported persistence boundary requires an explicit migration
path and human decision, not fixture age.

Deployment has a separate source floor: `UPDATE_SOURCE_FLOOR` in
`src/rcp/server_ops/deployment.py`, currently **v0.4.5**. Maintenance advertises
it as `update_source_floor`. Older data still runs the full migration chain,
but release gates cover stable releases at or above the floor plus frozen
fixtures. Raise the floor only after checking served installations. The
supervisor compares numeric major/minor/patch, ignoring build suffixes. Below
floor it changes no live state and instructs an update to the pinned floor,
then unpinning and updating again. Uncomparable identity requires receipt repair
or supervised adoption. Targets without the optional field keep their contract;
a target requiring a newer supervisor refuses until that supervisor is installed.

CI discovers every published stable `v*` release at/above the floor, exhausts
pagination, and fails on discovery errors or a missing floor. Each gets current-
and stale-cache checks. The installed matrix uses pristine Ubuntu 24.04 runners,
production bootstrap and wrappers, historical populated data, and real offline
scratch (uv hardlinks, Git, FIFOs, sockets, unreadable entries, dangling links,
empty directories and sparse files). Provider/Git receipts are synthetic.
A root-owned test hook substitutes the verified candidate bundle and port 18421;
all deployment owners remain production code. An injected failure checks restored
paths, types, bytes, modes, ownership, times, xattrs, links and hardlink groups,
then old-code HTTP, usable scratch, encrypted backup and successful retry.
The aggregate `old-data upgrade` check gates publication of those exact bytes.
It does not qualify privileged GitHub download, provider authentication, remote
roots, arbitrary old data or reboot recovery.

The checkpoint copier uses GNU `cp -a --preserve=xattr --reflink=auto
--sparse=always`, grouping roots by filesystem to retain hardlinks. Payloads live
outside captured roots on their own filesystems; a central catalog records them.
Ownership, modes, links, ACLs, xattrs, FIFOs and socket nodes survive; process
connections do not. External hardlinks become independent saved content.
A BSD fallback supports macOS development; Linux CI qualifies GNU behavior.
Copy plus `sync -f` must finish before readiness.

Rollback journals each root, renames the live tree to quarantine, renames the
saved payload into place and fsyncs both parents. Absent roots return to absence.
Recovery resumes from journal and paths; consumed snapshots are never replayed
over later work. Legacy subset checkpoints cannot stand in for whole-root copies.
Active operations finish with their original supervisor before self-update.

Preparation/pruning use a preparation lock; startup recovery uses the operation
lock so systemd can enter during an update. Retention follows confirmed successful
update (including already-current), supervisor update or restore, or explicit
`server prune`. It refuses unfinished operations or selected/current mismatch.

`limits.py` retains zero finished checkpoints, two newest completed application
trees plus live, 20 journals, and two completed supervisor/operator environments
plus selected/executing ones. Cleanup removes payloads/quarantines before journals,
including unpublished UUID workspaces and failed numeric builds. Receipts follow
release retention; a pruned build must be fetched and verified again. Unreferenced
root Python runtimes and interrupted staging are reclaimed; adoption is protected.

Failed diagnostics keep at most 20 logs, each bounded by the existing 8 MiB limit.
Success removes logs, download/fetch staging, temporary metadata, root uv cache
and maintenance captures. App installation uses per-release temporary files with
persistent uv caching disabled. Service Python and provider caches remain because
agent environments may use them. Journald owns service/backup log retention.
Unknown artifacts are reported untouched; pruning never follows symlinks into
unrelated data or imposes a byte quota on research or agent scratch.

### Selection and startup guard

The supervisor journals each phase before its effect. Candidate probation runs
as the service account with HTTP/background admission closed, proves exact build
and private live state, then stops. Durable `candidate_chosen` ends automatic
rollback authority; only then are selected receipt and ordinary service published.
Earlier failures after checkpoint readiness restore stopped roots and run the
old binary's own fenced proof. Failed candidate trees stay quarantined/reported.

`ExecStartPre` runs root `rcp-supervisor recover --startup` from local code,
journals and checkpoints, without fetching or requiring GitHub. It finishes
rollback before startup and never recursively controls its own unit. If the active
coordinator holds the lock while awaiting systemd, startup still requires a durable
choice and matching receipt/pointer. Unknown, inconsistent, corrupt or multiple
active journals refuse; contention alone grants nothing.

At/after `candidate_chosen` or `previous_chosen`, recovery verifies current chosen
state without replaying the captured proof or discarding accepted work. Fresh-host
rollback leaves uninitialized data stopped. `create_app` never interprets deployment
journals or chooses releases/rollback.

Read-only doctor reports selected/current/running identity, control availability,
root observational status and durable provider login state without writable SQLite.
Those projections cannot authorize startup. Team Settings reads release/backup
status, not private journals; absent public restore timestamps mean unavailable
history, not proof no restore occurred, and do not block other status.

### Source installation adoption and qualification

Paired-wheel adoption stages new integration without replacing the active
config, wrapper, unit or source pointer. A root-owned journal retains those files
and the full source commit; a startup guard is installed before service stop.
Current code discovers local roots from a disposable raw SQLite copy, then takes
one stopped whole-root snapshot, preserving cross-root hardlinks. Candidate
replacement roots must fit that snapshot, and a complete protected capture is
required before activation.

Before durable choice, failure restores raw data and original integration before
old source code starts. After choice, recovery preserves candidate data. This
explicit legacy adoption path does not change normal artifact-based updates.

Disposable QEMU qualification on Ubuntu 22.04/24.04 interrupts update and restore
at journal and individual-root boundaries, including repeated rollback and
network-free recovery. It requires changed Linux boot IDs; unavailable
virtualization is unqualified failure. Process restart tests do not prove reboot
recovery. Production cutover requires qualification and human promotion.

## Central checkouts and repository credentials

Each team project has one managed checkout per declared repository on its
execution machine: `rcp` owns server-local files; the configured SSH account owns
remote files. Creation/transfer prepares the whole set before registration.
Personal checkouts are not discovered or adopted.

Server-local roots use installed layout. SSH requests store an explicit absolute
central root or null default intent. An exact-account helper resolves default to
`<remote-home>/.local/share/rcp/projects`; its receipt pins the root and
`<project-id>/repositories/<alias>` descendants. Final review shows those paths;
operators recheck ownership, modes and symlink-free ancestry. Manifests retain
resolved paths, not permission to choose a new root.

Sources are GitHub.com or null (**Server only**). `GitHubRepositoryRef` accepts
only `https://github.com/<owner>/<repository>[.git]` or
`git@github.com:<owner>/<repository>[.git]` before any request or I/O. Owner is
1–39 ASCII alphanumeric/hyphen characters, alphanumeric at both ends; repository
is 1–100 ASCII alphanumeric, `.`, `_`, `-`, except `.` and `..`. One exact
`.git` suffix is stripped and identity lowercased. Credentials, queries,
fragments, encoding/traversal, other schemes/hosts/ports and extra components
refuse. All clone/settings URLs derive from that value; Enterprise/other hosts
need a trusted-origin design.

GitHub uses one write-enabled SSH deploy key per repository. A human adds its
public key with **Allow write access**; no GitHub user login, OAuth or token is
collected. Public repositories still require write proof. Private keys stay only
on the checkout account: installed `credentials/` locally or verified
`<remote-home>/.local/share/rcp/credentials/` remotely. A shipped helper validates
uid/home/modes; shell HOME and manifest overrides cannot choose the key root.
Keys never move between machines or enter SQLite, manifests, prompts, diagnostics
or backup. Public fingerprints and deterministic labels
`rcp:<space-id>:<project-id>:<repository-alias>` remain as recovery evidence.

With branch history, write proof creates, reads and removes a request-scoped ref
to an existing commit. Without it, the announced step initializes `main`, makes
empty `Start RCP project` as `RCP <rcp@rcp.invalid>`, records path/commit, then
pushes and reads back that commit as proof. Retry reuses it; non-forced pushes
and remote readback reconcile lost receipts. Competing pushes require human repair.

Server-only setup makes the same commit at the same checked path, with no remote,
key or push proof. Its code is **not backed up**; setup, Settings and backup
status say so. [Restore](#backup-and-restore) recreates an empty checkout.
**Why:** agents need Git history and branches, but starting research need not
require GitHub. An announced empty RCP-authored commit avoids unannounced changes
to human code. Keeping source Git outside research archives preserves their scope.

Cancellation needs an explicit request-owned key/checkout disposition. A public
key already granted on GitHub keeps **operator action needed** until revocation
is confirmed or the request is preserved for reuse. Removing a private key does
not revoke its grant. [Project deletion](projects-spaces-and-operations.md#deletion)
removes catalog/app data only; central checkouts, canonical history and deploy
keys remain for separate operator deprovisioning.

## Durable project provisioning

The unified project wizard creates durable requests before machine work.
Browser and desktop can create/review team setup; moving personal research
requires the desktop's authenticated two-space relay. Backend status is
**waiting for server setup**, **setup in progress**, **operator action needed**,
**ready for review**, **completed** or **cancelled**. UI renders it and the exact
next action, never infers readiness from subprocess exit or files.

Requests bind target space, name, sources/aliases, machines, state repository,
truth/default-run scopes, Auto-research ceiling, central-root intent, execution
profiles and preparation authorizer. Invalid text refuses before persistence/I/O.
New setup reserves a random `project_id`; transfer retains the existing id.
Reservation grants no canonical identity or writable home.

`rcp server project provision <request-id>` resumes path/mode checks, Git key and
checkout preparation, execution/provider readiness and write proof. The id is
correlation, not machine authority. It advances one durable stale-boundary-checked
step at a time and succeeds only after reading **ready for review** back.
Stops name exact GitHub grant, account/provider/SSH repair and resume actions.
Empty-repository initialization is announced before any commit/push.

Every authenticated team member may inspect requests. Only the direct new-project
preparation authorizer can cancel through the member API, only while waiting
with **nothing to remove**; completed cancellation is idempotent. Once machine
work begins its owner must record cleanup/reuse disposition.

Direct creation never adopts or archives retained research. Existing identity or
Patch history stops before overwrite: personal history directs **Move to team
space**; other conflicts need a deliberately cleaned/different repository.
Review checks again. Transfer alone may reuse an archive-proven matching prefix.

Desktop **Run setup now** requires a proven saved operator SSH route, separate
from member transport: direct `rcp` or named operator with `sudo -n -u rcp -H`.
It uses system SSH configuration/agent, never collects a key or sudo password,
bounds progress and rereads the authenticated request afterward. Interactive
steps open the fixed command in Terminal. Otherwise it shows a copyable operator
command; a browser cannot run machine steps.

Machine preparation creates no project. Final review shows paths, readiness and
work to settle. New-project completion at
`POST /api/project-provisioning/requests/{request-id}/complete` requires the exact
review digest and a current member; the preparation authorizer must remain
enrolled. It rechecks configuration, all six profiles, checkout paths, Git/provider
proofs and retained inputs without rerunning preparation. It renders the manifest,
appends one system `created` identity with the reserved id, and seats the reviewer.
Manifest, identity Patch, registration, seating and completion have recoverable
boundaries. Retry accepts only that request's exact one-Patch identity prefix;
stale digest, changed inputs, unsafe paths or conflicting identity refuse.
Completed confirmation returns the same request/project.

Settings can request one added repository or connect one server-only repository
to GitHub through the same command/review, without provider setup. Add prepares
a new checkout and publishes the reviewed descriptor/truth choice through a
human approval transition. Connect preserves checkout/manifest, adds the reviewed
remote and key, and pushes recorded `main` only if remote main is absent or an
ancestor. Divergence requires a human merge before retry; RCP never merges or
force-pushes. The exact storage/review contract belongs to
[Repository and truth scope](projects-spaces-and-operations.md#repository-and-truth-scope).

Transfer links bind a checksummed nonsecret source summary: schema/codec versions,
sources and aliases, state repository, truth provenance and manifest digest.
Target negotiates a common version before preparation and rebuilds configuration
without source paths/homes. Source rechecks summary and preparation revision
before fencing; drift/incompatibility leaves it writable and requires review.

One final desktop review records target admission first, then source release,
through independently authenticated backends with their own actors and request
receipts. User ids need not match. Target-only confirmation leaves source writable
and creates nothing. Source release waits for legacy artifact imports. Import
must validate both receipts and cannot supply human authority.

Each linked side commits SHA-256 of its independent random 256-bit one-time proof;
review binds both commitments. Source proof is revealed only in the sealed
post-fence archive; target proof only after activation through the native
permanent-token route. [Transfer](#personal-to-team-transfer-archive) owns relay,
consumption and cleanup. **Why:** a client-relayed JSON receipt, request id,
archive path or successful CLI exit cannot prove the other space's transition.
These proofs protect product authority, not storage from root or its owning account.

## Personal-to-team transfer archive

Transfer requires the two human confirmations and source configuration/proof
binding in [provisioning](#durable-project-provisioning). It preserves project
identity while changing home; it never clones either space's authority database.

After revalidation, the source closes paused standalone attempts as interrupted
in one settlement transaction, with request-bound events/receipts. Original
output, native-session evidence, finish timestamp and scratch remain at source.
The final review discloses this closure. Queued/running/pausing or episode-owned
attempts are not administratively stopped. Any live task, episode, watcher,
report, child or delivery refuses settlement until its normal owner settles it.
Read-only export never changes task status.

One versioned, checksummed archive carries:

- Project identity, exact main/branch heads, immutable branch metadata, Patches,
  merge receipts and source manifest as provenance, never materialized outputs.
- Typed RCP chats, Paper draft including conflict ancestors, canonical
  introduction and opaque safe regular `.research/facts/` files.
- All finished human-visible tasks, events, receipts, usage and stopped
  episode/watcher/report history, including child, message, recovery, lifecycle,
  Apply-result and inert-command records needed to render them.
- Every database-referenced artifact version and saved live snapshot through one
  typed inventory; older inline-report and repository-kept formats stay readable.
  A deleted episode report travels as its lineage row without bytes, and the
  target records its artifact as deleted. Older targets refuse such a project.
- Complete project-matched native provider conversations and, when required or
  explicitly chosen, reviewed Git commits and their reachable history.

A schema inventory test requires explicit classification of every project-linked
table. Membership, invitations, authentication, skill inventories, reconciliation
watermarks, operation leases, resumable stages/sessions, pending wake fields,
wrap-up paths, temporary input attachment bytes, expired unkept artifacts,
credentials, machine configuration and caches do not transfer.

Canonical chats retain stable chat/message ids, text, times, provider/model
labels, graph receipts and display-only attachment metadata. They clear native
session ids, execution machine/cwd and unmapped operation bindings.
`writing_sessions` and `chat_session_contexts` are excluded. Imported tasks keep
honest terminal status and answer but are history-only: no Pause, Resume, Retry,
graph repair, action-queue entry or executable source-session identity. Future
work starts under target configuration.

**Why:** one sealed archive provides one inspectable compatibility and retry
boundary. Raw database copying would import unrelated space authority and unsafe
continuations. Finished operational history explains the research without
pretending source processes can resume.

The existing conversation index selects native transcripts best-effort by
recorded cwd and declared repository paths, using each saved profile's exact
local/SSH account and provider roots. It copies clear matches byte-for-byte and
rechecks cwd; unmatched, rewritten, malformed or unreadable files get a
nonblocking count/diagnostic. There is no completeness claim, history inference
or manual classification screen. RCP-owned chats travel separately.

**Why:** normalizing native files loses provider-specific evidence; filtering by
`last_refresh_at` mistakes an overlap watermark for exact coverage. Passive
session ids, paths or sensitive printed text may remain in historical bytes,
but never become execution bindings.

Imported files live read-only under
`<RCP_DATA_DIR>/project-sources/<project-id>/provider-history/<provider>/`, named
by content hash with a sealed inventory. They are durable project data, not
canonical state, cache or a provider home. Seed/Refresh reads them beside live
provider roots and retains the overlap watermark. SSH execution stages only that
inventory into immutable task input; Resume/prepared-context reuse verifies its
fingerprint. Missing or changed staging uses the visible clean-retry path.
After sealing, missing, altered, writable, symlinked, special or undeclared source
entries are corruption and fail visibly; best-effort selection has ended.

Target configuration comes solely from reviewed readiness proofs. Historical
repository/machine aliases, state repository and truth provenance stay stable;
paths, hosts/accounts, binaries, native roots and execution profiles are rebuilt.
Main and every branch replay against it in isolation. A branch's authorizer must
match canonical home at its immutable base, not merely today's home, and gains
no current write authority from that history.
Retained Git `.research` must be a byte-identical coherent archive prefix with
no later external commit, unknown durable entry or alias conflict. Validation
binds review, source/link/archive identities, rebuilt manifest, retained-prefix
fingerprint and replay heads into the importer receipt; conflicts never overwrite.

**Include local unpushed commits** is off by default for GitHub sources. Without
it the prepared GitHub checkout is used and review warns about unpublished source
commits. With it every repository HEAD is bound to source configuration and both
confirmations, rechecked at export and carried as a self-contained Git bundle.
A no-origin repository becomes server-only and always requires its reviewed
commit/bundle; mixed projects bundle only captured repositories. No GitHub push
occurs. Other branches, dirty/ignored files, external outputs, Git configuration,
credentials and hooks do not travel.

Bundles require schema/codec `2` / `rcp-transfer-v2`; GitHub-only transfers without
bundles retain byte-compatible v1 with absent commit fields. An incompatible
target refuses before release. Changed HEAD or choice needs fresh review; old
commitments and sealed archives remain valid.

The importer validates bundles in isolation and imports objects without network.
A changed GitHub revision becomes detached HEAD, retaining origin and branch
refs; an already-matching checkout retains attachment and untracked/ignored files.
Server-only import attaches the commit to `main` without a remote. Missing bundles,
tracked changes, unsupported Git layouts or tracked `.research`/`.recovery` refuse.
Create a branch before new commits from detached HEAD. Interrupted multi-repository
import resumes the same archive and remains inactive until full activation.

Before mutation, import checks the exact regular-file inventory, sizes/hashes,
identities, heads, references, configuration and excluded fields. One SQLite
transaction inserts mapped operational rows and configuration/import receipts.
Concrete atomic owners then publish the manifest, canonical history, chats,
Paper, facts, artifacts and provider files. Legacy kept files restore to their
old location for background import. No project or writing-session row is created.
Readback and replay produce one completion digest. On interruption,
`database_imported` can coexist with partial matching files, but the project is
invisible. Retry uses stored configuration and exact digests, not a new inventory
of partially published state. Activation needs the complete receipt.

Captured artifacts stay viewable/downloadable; editing uses **Edit in a new
session**. Unimported legacy stage artifacts are explicitly unavailable. Artifact
projection and content/action admission use the availability contract in
[Paper, artifacts, and viewing](paper-artifacts-and-result-views.md), never source
stage pointers or client inference.

`source_released` fences new human tasks, episodes, Auto-research, Experiments
and merges inside their admission transaction; HTTP also holds the project
operation lock. Authorized continuations may settle, but export refuses remaining
live work. Canonical/file capture holds the workspace transaction and remote
lease when applicable. Home change records both human actors.

After home change, the source atomically seals mode-0600
`<RCP_DATA_DIR>/transfer-exports/<request-id>.rcp-transfer`. The post-fence source
proof contributes to its bound digest and is consumed, not imported. Every retry
rehashes and streams the same file; it never reselects provider history.
The original confirmed configuration/head remains available for recovery;
source-release re-entry finishes settlement/capture without another request or
ownership confirmation. Ordinary Delete remains unavailable.

The native relay verifies the pinned personal backend and fetches that exact
request export with its pinned-instance header. It streams bounded chunks to
stdin of one system-SSH child running
`rcp server project transfer-import <request-id>`. Through the private socket,
the CLI gets expected digest/size and a request-derived upload lease; it accepts
no arbitrary archive path and never opens SQLite. It writes a same-directory
0600 `.partial` in `transfer-inbox/`, publishes by no-overwrite hard link to the
request filename, verifies and removes the partial. Exact retries consume and
verify stdin without replacing the final file.

The service owns upload states `active`, `complete`, `consumed`, `invalidated`.
Completion rehashes bytes; a separate control call decodes into disposable staging
and validates human receipts, fence, readiness, modes, digest, manifest and source
proof before invoking import. Upload or CLI success is never project authority.
After readback/replay, one transaction registers the project, seats the target
confirmer, completes provisioning and activation receipts, marks
`target_activated` and consumes the upload. Only then is the project visible and
the target proof retrievable. Cleanup removes the verified inbox and stage;
retry after activation refreshes catalog state and cleans those exact files.
Pre-activation failure retains the completed inbox for repair.

The native permanent-token route returns the precommitted activation proof only
to the original current target confirmer for that completed request. The relay
returns it directly to the pinned source. Source cleanup holds the project lock,
validates the sealed archive, consumes proof, retires the catalog row and
revalidates before unlinking the exact file. Missing/corrupt bytes stay visible
for repair; consumed-proof and retirement receipts distinguish completed cleanup
from premature loss. Historical membership/invitation audit rows remain.
The public cleanup acknowledgment lets the target erase its raw proof.

No archive bytes, raw proofs or other-space credentials enter browser state,
URLs, argv, shell strings, logs, credential stores or Tauri IPC. Web commands
carry only validated request ids and progress metadata. Tauri builds no `scp`,
`mv` or shell pipeline. Without a proven operator route, the desktop exports one
protected local file and shows bounded Terminal commands.

Update refuses an active upload immediately with admission still open for
completion. Preparation retains receipt-backed complete inbox files, ignores
consumed records and rejects untyped leftovers; whole-root rollback retains the
entire inbox independently. Team backups exclude inbox and personal exports.
Restore invalidates old leases and unfinished machine state, marking nonterminal
incoming requests **operator action needed**. An `archive_bound` re-entry checks
the restored revision, rebuilt review digest, original current confirmer and
same archive, then issues a fresh relay lease. Later phases remain fenced until
their proof/cleanup owner revalidates them. Target restore never reverses a
committed source home change.

## Backup and restore

Protected backups are encrypted research/control-plane archives, distinct from
whole-root update checkpoints. They do not pause dispatch or Apply and never
claim an unreachable project is protected. [The operator guide](../server.md#back-up-the-team-server)
owns configuration, capture and restore commands.

Capture uses one immutable online SQLite snapshot and inventories only its
referenced durable files. It includes main/branch Patch histories and exact heads,
branch metadata/merge receipts, typed RCP chats, optional Paper introduction,
safe regular facts, referenced artifact versions/live snapshots and imported
project-owned provider histories. Artifact capture holds the pruning lock.
Legacy repository-kept candidates remain capturable while pending import;
stage-only candidates do not. Restoring bytes never accepts a candidate.
UUID4 and UUID5 task identities are both valid inventory inputs.

The app-data inventory is closed. Live SQLite/WAL/SHM, process metadata, bootstrap
locators, display/source/session caches, temporary chat attachments, run/transfer
stages, personal transfer exports, source Git, deploy/SSH/provider credentials,
live provider homes/logs and materialized graph outputs are excluded. Each new
direct child needs explicit classification or capture becomes visibly partial.
Every project gets a sealed imported-history inventory or explicit absence;
orphaned, unsafe, missing or hash-mismatched imported history fails visibly and
never substitutes a live provider home.

Project-file capture takes no canonical append/chat/publication or remote refresh
lock. It reads chats through complete typed-valid JSONL boundaries and mutable
Paper/facts/kept files through bounded stable reads. Churn, unsafe or missing
referenced entries, unknown durable roots and unreachable projects are uncaptured,
while healthy projects remain usable. Later replacements never count as partially
captured files. Inventory and capture use no Git fetch or provider credentials.

Archives bind every entry's size/digest and stream through fixed-size buffers.
There is no arbitrary scientific-history size ceiling; insufficient capacity is
an explicit partial/failure result. Ciphertext is atomically published and fully
read back with an immutable receipt. Failures name safe categories and relative
components (including state, checkout, history and SSH), never raw exceptions or
absolute locators. CLI/doctor report these causes; pre-update failures become
`backup_warning`. Optional operation-bound diagnostic sidecars preserve causes
without changing strict older receipts; absent sidecars are not reconstructed.

Each protected team project also binds a nonsecret recovery descriptor: current
manifest settings/machines (including each machine's access hint and compute
block), effective repository sources/aliases, resolved checkout
paths/routes, and public old key labels/fingerprints. Completed provisioning proves
checkout location and grants; it does not freeze later Settings edits or machines
without repositories. Missing, credential-bearing or inconsistent descriptors
make the project uncaptured. [Effective repositories](projects-spaces-and-operations.md#repository-and-truth-scope)
fold completed create/add/connect requests.

GitHub checkouts retain origin/commit checks. Server-only checkouts have no remote;
backup names each as **code not backed up**, even if research capture is complete.
Restore initializes missing server-only destinations with `git init -b main` and
empty `Start RCP project` as `RCP <rcp@rcp.invalid>`, then publishes research.
Private `checkout-recovery/<project-id>.json` records replacement commit and public
key evidence, bound to repository/machine; a later connect supersedes its proof.
Backup embeds the resolved proof, not the raw receipt, and restore reconstructs
it without changing historical provisioning reviews.

Skill comparisons preserve manifest-declared fields, including omissions, rather
than release defaults. Older descriptors keep bytes/hashes and compare only fields
the current manifest declares; other checkout/configuration authority stays exact.

Backup accepts native X25519 `age1...` recipients and upstream age >=1.0.0,<2.0.0,
not plugin, SSH, passphrase or post-quantum recipients. Destination is one explicit
writable local/mounted directory. RCP owns publication, integrity and retention,
not cloud/SSH transport, and infers no off-host durability from its path.

The machine config's strict `[backup]` table contains only destination, recipient,
schedule and retention, independent of restored SQLite. Configuration defaults
to 02:00 server-local time and 30 archives and requires explicit confirmation.
Without `--recipient`, it atomically creates/reuses root-only 0600
`/etc/rcp/backup-recovery.agekey` and a root-owned 0644 `.pub` sidecar. Only the
public recipient enters config. The sidecar detects a missing managed identity;
missing, corrupt, unsafe or mismatched keys refuse, never regenerate silently.
An external recipient must be supplied exactly again; rotation is not implicit.
Private key text never enters CLI arguments or progress.

**Why:** the default makes routine capture and same-host restore simple. Whole-host
recovery still needs both archive and identity retained elsewhere, or an externally
managed recipient. RCP cannot infer that either is protected off-host.

One root-owned operation lock serializes install/configuration. Before unit
changes, configuration records the complete public intent atomically, fences any
loaded timer and proves it disabled/inactive after reload. Interrupted work must
finish and read back that exact intent before new configuration proceeds. Only
matching config/timer/systemd readback clears it. The timer enables only after a
successful first backup/readback; failure leaves it disabled.

Nightly/manual/pre-update captures share retention. Keep the configured count
plus the newest complete archive if outside it: at most `retention + 1`
archive/receipt pairs. Fixed status/diagnostic files remain bounded. Only the
newest failed plaintext capture remains; a completed protected or partial archive
removes prior stages. Retry reconciles publication intents under the backup lock
before reclaiming recognized temporary files/orphan receipts. Research and agent
scratch are never truncated by this policy.

Restore is an operator-confirmed workflow over the displayed configured
`RCP_DATA_DIR`, initialized or fresh. It requires assurance that the old space
cannot resume serving, integrity/replay checks, and supported archive/schema
identity before mutation. Future formats refuse with an update requirement.
Default decryption uses the fixed root identity; `--identity-file` reads an
external file only for that run, never raw key text in environment or output.
Restore does not redirect systemd to another data root.

Root-owned preparation outside app data binds archive and selected release,
operator confirmations and progress. Protected plaintext is readable only by
root and service group. The app's offline worker owns archive validation,
lifecycle detachment, checkout recovery, roster policy and replay; the supervisor
owns journals/selection. A credential/review pause returns an existing server to
service; retry captures a fresh rollback boundary and revalidates preparation.
A fresh host stays stopped without dummy team initialization.

Restore names old source/project deploy keys, server-to-remote SSH grants and
provider logins to revoke or prove destroyed, then replacement routes/grants.
It never collects those secrets or performs external login/revocation.
Permanent snapshot-time member token hashes survive, but browser sessions,
bootstrap/enrollment and pairing codes are invalidated. Before serving, the
operator reviews capture time and exact member roster. RCP cannot know later
revocations. Known-stale tokens require a newer safe archive or explicit offline
member removal with another enrolled member and no orphaned project. The normal
removal transaction/fence applies; operators cannot impersonate or mint a token.
A sole stale member leaves restore stopped for separate identity recovery.

`space_id` survives. All prior tasks become history-only and captured active work
becomes interrupted; native session/stage bindings, writing sessions and chat
context indexes are cleared. Answers, receipts, chat and Paper remain readable;
new turns start fresh only after provider readiness. Restore detaches nonterminal
episodes, reports, watchers, deliveries, child admissions and automatic recovery
before startup. Proof must show that no old turn, check, retry or graph mutation
launches. The same transaction rotates the resource watermark epoch.
Unfinished provisioning/operation leases lose old step claims and become
**operator action needed**, never auto-resume machine authority.

Notification preferences/devices/outbox/observations participate in schema and
restore validation. Restore detaches devices and resets observations for a quiet
baseline. Transfer discards only that project's preferences/outbox/observations,
not space devices or other projects. The sender starts after recovery/effect
fence release; maintenance drains it and reopening resumes it. It never mutates
canonical graph history.

Every captured checkout is reconstructed before `.research` publication, using
its descriptor and normal exact-account checkout/key helpers. GitHub recovery
generates new local/remote repository keys; SSH routes need renewed verification.
Server-only recovery follows the empty-repository rule above. Existing retained
research must be byte-identical archived entries without later commits. Conflicts
refuse. All captured file groups and replay must validate; uncaptured projects
stay visible but unavailable. Bootstrap locators regenerate from descriptors and
catalog paths rebind; old absolute locator files are not authority.

Protected restore uses deployment's journal and whole-root publication protocol.
Before replacement it backs up an initialized target, closes admission, stops it
and snapshots every destination, including absence. Additional local project,
artifact and legacy view roots are captured. Candidate trees preserve unrelated
existing files with the privileged copier before overlaying validated content.
Divergent same-host remote history refuses because local rollback cannot undo it;
fresh remote reconstruction requires explicit old-authority review and grants.

Root publishes under closed admission, runs the app's fenced proof of replay,
imported history, roster and detached lifecycle, then durably selects and serves.
Pre-selection failure restores prior initialized service or leaves a fresh host
stopped; startup recovers interrupted publication. Post-selection recovery keeps
accepted work. Provider sign-in is separate: missing login blocks new tasks,
not restoration or reading of research.

## Agent browser host runtime

Browser installation is explicit. The execution account needs Node.js 20 or
newer and npm; RCP itself still starts without them. Doctor and machine cards
use the same readiness service. Doctor probes the installed service account,
including its login environment, rather than root's tools. Browser readiness is
optional and does not make the core server dependency check fail.

RCP installs a pinned Playwright CLI and Chromium into its tools directory. Agents
reach it through an RCP-owned `playwright-cli` launcher in `tools/bin` that runs
the Node readiness checked, never whichever `node` comes first on the shell's
`PATH`; the pinned Playwright refuses Node older than 20.
Profiles, configurations and owner state live in the sibling browser directory.
Both roots are excluded from protected backups and remain RCP-owned protected
storage under ordinary agent write scopes. SSH hosts use the private account
RCP root. Browser cookies and live process handles are never restored.
