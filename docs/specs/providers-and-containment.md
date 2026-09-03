# Providers and containment

This specification owns provider capabilities, launch construction, exact
project write scopes, remote execution, Seed/Refresh ingestion, provider
readiness, skills, and durable launch receipts. Semantic graph authority is
separate and defined in [Authority and Proposals](authority-and-proposals.md).

## Fixed task capabilities

Run policy has explicit modules for Seed/Refresh, Discuss, Work,
Experiment-loop, Auto-research orchestration, graph merge, correction, and Paper
coaching. Shared launch, event, stage, and receipt plumbing never chooses policy
from a generic surface discriminator.

Capabilities are fixed in code:

- **Seed/Refresh** reads configured provider logs and project repositories and
  writes only its RCP scratch Patch.
- **Discuss** has writable conversation scratch, read-only project reasoning,
  public-web tools, and no active Patch contract.
- **Work** has noninteractive project operational tools, public-web tools, exact
  project repository write roots, and one optional semantic Patch.
- **Experiment-loop** uses Work-like operational access with its dedicated
  focused-Experiment graph and watcher contract.
- **Auto-research orchestrate** uses Work-like project repository access plus
  its dedicated staged command client and orchestrator graph profile.
- **graph merge** is graph-only; it writes its scratch candidate and receives no
  repository write roots.
- **generic graph correction** rewrites only retained scratch output; Work-like
  correction retains the same native session and the same exact Work write
  scope so it can repair reflection without repeating operational effects.
- **Paper coach** is read-only and has no graph, draft-write, or Apply channel.

The manifest and selected skills may choose execution details or add guidance;
they cannot widen or narrow these capabilities.

## Provider runtime selection

Each project agent profile selects a provider-owned runtime. An omitted value is
backward compatible: Codex uses `exec` and Claude uses `stream-json`. Provider
readiness exports the allowed names and the one an omitted value resolves to;
project setup and Project Settings render those answers, and the backend
validates the saved provider/runtime pair. No surface derives that default from
the order of the exported names. A profile always carries a resolved runtime, so
no selection surface stages or shows an empty one. A task request cannot
override the profile's runtime. The selected profile runtime applies to every
capability launched through that profile, so one `node_chat` choice covers both
Discuss and Work while their distinct capability contracts remain fixed in code.

Codex additionally offers `app-server`. RCP starts one fresh stdio app-server
process for each provider turn and uses the same shared local/SSH launcher,
process control, invocation gate, exact permission profile, event normalization,
usage accounting, and cleanup as `exec`. Provider threads remain persisted by
Codex, so a local app-server-created conversation can appear in Codex Desktop.
That visibility is provider-owned inspection; RCP does not order, take over, or
coordinate Desktop tasks.

The preferred runtime is chosen anew from the current project profile for every
RCP task invocation, including a continuation of an existing native session. A
native session is not permanently bound to the runtime that created it. RCP
records the runtime actually used on the task and, for Paper, on the writing
session.

Codex app-server may fall back silently to Codex exec on the same local or SSH
machine only while failure is known to precede delivery of the new prompt. RCP
checkpoints the actual runtime before the write that can deliver that prompt.
Once `turn/start` may have been accepted, failure is retained as an interrupted
or failed attempt and RCP never retries the prompt through exec. This rule is the
same for fresh and resumed conversations. Silent means the human's turn is not
failed, not that the cause is lost: the passed-over runtime and its failure are
recorded as a task diagnostic, and the runtime that did run is named on the task
and on the Paper writing session, which is where a substitution becomes visible.

Containment differs by runtime because the transports differ. `exec` refuses the
whole user config file and its execpolicy `.rules` files with `--ignore-user-config`
and `--ignore-rules`. `app-server` accepts neither flag, so RCP names each
capability-bearing config key instead, and cannot disable `.rules` at all.

## Cooperative project write containment

Work-like provider launches are guarded against accidental writes into another
project. The users, team members, prompts, and project inputs in this model are
cooperative. RCP does not claim hostile same-account process isolation,
cross-project read secrecy, a general OS sandbox, network confinement, or
resource supervision.

RCP resolves one strict `ProjectWriteScope` before every Work or orchestrate
launch. It binds:

- durable `project_id`;
- execution machine and host/account identity;
- capability;
- exact task stage and workspace;
- exact admitted run-scope repository aliases and canonical roots on that
  execution machine;
- protected RCP-owned write paths; and
- a stable fingerprint of the canonicalized contract.

Only RCP derives the scope from the manifest, project catalog, repository
pointers, task/episode lineage, and run scope. A browser, prompt, request body,
provider, or staged file cannot add a root. Scope construction requires a
complete inventory of every registered project manifest; if any registered
manifest is unavailable, the Work-like scope and launch fail closed.

The workspace must be within the exact task stage. Repository roots must be
registered to the same project, alias, execution machine, and host, must exist
and be writable by the execution account, and are deduplicated without replacing
them with a common parent. Local and remote resolution use the paths on the
actual execution machine.

That host comes from the project catalog and manifest. It is never read from the
repository pointer handed to the agent: a pointer's host states how that agent
reaches the repository, so it is empty for a repository on the execution machine
and says nothing about where the machine is. What the scope requires of a
pointer is its machine and its path, and the path is compared after both are
canonicalized on the execution machine.

The scope rejects another project's repository, a parent containing several
projects, the application data directory, SQLite, canonical `.research`, the
execution account's home directory, and broad temporary directories. Provider
authentication/session/cache storage may use the provider's own runtime
exceptions; those exceptions are not general project roots.

## Provider enforcement

### Codex

Work and orchestrate use Codex's native noninteractive project permission
profile: the exact workspace plus admitted repository roots, never the dangerous
bypass flag. Fresh launches set the exact working directory; native resume uses
the provider's supported configuration path while preserving the same scope.
Public web search remains enabled. Canonical `.research` nested in a writable
root is refused as a write and kept readable, because RCP stages its `graph.json`
and `research.md` as required run context; Codex treats a denied path as
unreadable rather than unwritable.

### Claude

Work and orchestrate use Claude's supported unattended `dontAsk` mode with an
RCP-authored strict settings allow-list for the exact workspace and admitted
repository roots. They never use `bypassPermissions`. RCP suppresses user
settings and unrelated MCP configuration for this enforced launch. Public
WebSearch and WebFetch remain available under the provider contract.

### Version failure

Provider profiles own the minimum supported CLI contract. If the installed
supported version cannot enforce exact unattended roots, the launch fails with
a provider-compatibility diagnostic. A selected Codex app-server version or
startup that fails before prompt delivery may use the explicit exec fallback;
exec must still enforce the same capability and exact roots. RCP never restores
broad bypass access or treats prompt wording as containment.

## Continuation binding

Every launch receipt records the project, execution host, capability, canonical
writable roots, scope fingerprint, provider enforcement mode, and whether the
launch was fresh or resumed.

Before Resume, Retry, watcher wake, Work graph/watcher correction, child
continuation, or report-independent Work continuation, RCP recomputes the
current scope and compares project id, host, exact stage, target, and root
fingerprint with the durable binding. A cross-project session/stage, relocated
repository, incompatible run-scope change, or missing root fails before provider
launch. Legitimate relocation or scope change starts a fresh task/session; it
does not widen an existing native session.

The same resolver covers ordinary Work, Auto-research root, child Work, child
Experiment, watcher wake, and correction paths. There is no permissive fallback
for a continuation's project, host, stage, graph target, or write scope. The
pre-prompt provider-runtime fallback above changes none of those bindings and
resumes the same native session id.

## One graph output channel

An agent writes `patch.json` only in its exact RCP-owned stage. That file is the
sole graph-change channel. Work repository edits carry operational authority,
not graph authority, and canonical `.research` stays outside agent write roots.
RCP never extracts a Patch from stdout, an answer, provider directive, artifact,
URL, or repository file.

The labelled final assistant message is the answer. Provider traces, reasoning,
tool output, and the Patch verdict remain separate. A rejected Patch does not
discard an already-produced answer.

Every patch-producing Seed, Refresh, or Work stage carries an RCP-staged Python
validator client. It exchanges bounded request and response files through the
writable workspace while RCP polls locally or through the existing SSH run stage,
prepares the candidate against live current state in process, and records each
check. Client exit codes distinguish valid, semantically invalid, and validator
unavailable, so a transport failure can never become a correction loop.
Validation stages operations in their written order against earlier valid
operations while retaining whole-patch node and edge lookup for legal forward
references; it never reorders operations. A validator self-check is not a
reservation: Apply re-prepares RCP-owned bookkeeping and reruns the same semantic
validator against current state while holding the canonical append lock, so graph
movement between response and Apply is not by itself a rejection.

## Durable task lifecycle

Agent work belongs to the backend, not a browser view. Before execution, RCP
persists the task, task attempt, authorizer, capability, target, exact stage,
provider identity, and write-scope binding. Immediately before prompt delivery
it persists the actual provider runtime. Provider events retain labelled
answers, native session ids, usage, diagnostics, Patch results, and launch
receipts.

Pause, Resume, Retry, and correction form explicit parent/child attempt chains.
They retain task mode, graph target, capability, host, stage, and external-effect
diagnostics. A failed run retains its scratch and Patch text for bounded
same-session repair and normal retention; RCP does not delete evidence merely
because validation or transport failed. Age-based cleanup first excludes exact
stages owned by active tasks, committed native chat sessions, live episodes,
pending or running report wrap-ups, and unexpired temporary result views. One
storage projection supplies the same ownership and required-stage decisions to
cleanup and update checkpointing; terminal debris follows normal retention.

Seed and Refresh repair through a generic correction ladder: rescan the retained
stage for the Patch, then hand validation errors back to the same live session
for at most two scratch-only rounds. A reused stage still holds the previous
attempt's `patch.json`, so RCP fingerprints that file before each correction
launch and refuses an unchanged one, handing the unchanged-file diagnostic
forward rather than revalidating the same bytes. A graph-level rejection is never
retried. Work instead repairs through same-access `work_patch_correction`, which
retains the original native session and write scope and changes only the
instruction.

Unrelated tasks may run concurrently. Turns in the same conversation and native
stage do not overlap. Canonical append remains serialized by the graph target's
state workspace.

## Local and SSH execution

The provider runs on the machine that owns the selected repository pointers.
Local and SSH launches use the same capability and write-scope contract. Remote
command construction carries remote canonical roots, never local substitutes or
a broad remote working directory.

The remote run stage owns exact path validation, process/event wrappers,
scratch transfer, and recovery. A task resumed remotely must prove the saved
host and stage. SSH or provider transport failure is reported as unavailable,
not converted into semantic correction.

### Compute connections are resources, not execution profiles

A project may configure named local or SSH compute connections as non-secret
resource metadata. A compute connection is not a `MachineConfig`, never supplies
`run_on`, and never changes where a provider launches. Local compute means the
machine on which the current agent is already running; an SSH compute target is
reached from that same machine. Attaching or detaching one does not relaunch,
move, or replace the provider, and it does not expand repository or write scope.

The backend probes every configured connection from every machine currently
selected by an agent execution profile. Readiness is therefore keyed by
`(agent execution machine, compute connection)`, not stored as one global fact.
SSH probes use the ordinary OpenSSH credentials already configured for the
execution account, batch mode, a bounded connection timeout, and strict host-key
checking. Strict host-key probes use a direct OpenSSH transport with connection
sharing disabled, so an existing multiplexed master cannot bypass the check.
Results distinguish reachable, unreachable, authentication failure, and host-key
failure. An authentication or host-key action names the exact agent machine on
which the operator must repair ordinary SSH state. RCP has no key or password field
and never imports, stores, stages, or transmits those credentials.
Only explicit readiness refresh runs these SSH probes. Normal readiness polling
returns the last authoritative matrix without network work and without waiting
behind an explicit refresh of a slow target, and any connection or
execution-binding change invalidates that matrix. Provider readiness and compute
status invalidate independently: resolving one provider path leaves an in-flight
compute probe free to apply, and a compute change leaves recorded provider
readiness in place. Probe diagnostics cross one redacting, single-line boundary
before projection, and the backend exports the status label and tone consumed by
every UI surface. A changed connection never inherits the old target's status;
if the post-save refresh fails, Settings keeps the saved metadata, leaves status
empty, and reports that the refresh failed.

Projects configure at most 32 compute connections and a turn selects at most 32
ids. These shared schema limits apply before manifest persistence, probing, chat
record storage, or prompt assembly. Unsaved compute form metadata remains only in
the live Settings component; local settings drafts deliberately omit it and
discard the briefly shipped v3 field during migration.

The human attaches configured computes through the conversation composer. The
task request and canonical chat record persist the selected ids so reload and
recovery render the last truthful active set. Only selected profiles are eligible
for agent context. Admission resolves those ids once into a bounded, immutable,
server-owned non-secret profile snapshot stored with the task; client-supplied
resolved metadata is ignored. Fresh and resumed prompts, Retry, recovery, and
generic or Experiment watcher wakes use that snapshot rather than re-reading a
later manifest. Watcher continuations persist both the selected ids and resolved
snapshot; older rows default to an empty selection. The first master/task context
describes the names, local or SSH access route, and optional non-secret access
hint. Later ordinary additions and updates send the same bounded profile metadata
in a concise delta so a resumed native session can use a newly attached or
retargeted resource; removals send only the display name. Compute metadata never
grants authority and never includes credential contents or paths.

### Team execution accounts and credentials

The team backend remains the one owner of every provider call, whether the
provider runs on the RCP server or over SSH. A local team run executes as the
Linux `rcp` service account and uses whatever provider-native authentication is
already present for that account. A remote team run uses the exact SSH
host/account selected by the project profile and whatever provider-native
authentication is already present there. The remote account need not be named
`rcp`; it must be explicitly configured and reachable from the server's service
account.

RCP does not perform provider login, store provider credentials, switch accounts,
refresh tokens, or create alternate provider homes. The operator uses each
provider's own login command directly as the target execution account. RCP's
server CLI checks executable, version, provider-reported authentication status,
configured runtime, explicit model catalog entry, reasoning effort, and the
provider-owned minimum version for that profile through the same launch
abstraction used by tasks. A catalog failure cannot approve an explicitly saved
model, and an unexpected implementation error fails the command rather than
being relabelled as a missing install. The later provider call uses the same
native authentication and version rule. A failed check names the provider,
machine/account, and provider-native action to perform, then waits for the
operator to do it outside RCP. Provider credentials never enter a project
manifest, provisioning request, prompt, backup, or member's desktop credential
store.

For execution on the server itself, provider discovery first uses the service
process `PATH`, then the executing account's conventional
`~/.local/bin/<provider>` location. This covers provider-native per-user installs
without borrowing another Linux user's shell configuration. A successful check
still records and later invokes the resolved absolute executable path.

The check always resolves an existing configuration boundary:
`rcp server provider check --request <request-id>` checks the intended profiles
of one provisioning request, while `--project <project-id>` checks the stored
profiles of one existing project. Exactly one selector is required. The command
does not accept an arbitrary host, account, executable, provider home, or runtime
that could bypass the request/manifest contract.

The service owns resolution and probing through its private control socket; the
`rcp` CLI never opens SQLite beside the running process. Planning is read-only
and binds one SHA-256 boundary over the durable request revision or stored
project profiles. Each check revalidates that boundary before a subprocess and
publishes a safe refusal if it changed. A successful request check stores only
the resolved absolute executable path, bounded version, durable runtime id,
observed OS account, and check time. Rechecking that request uses the stored
executable instead of rediscovering `PATH`. The provisioning projection exposes
those nonsecret proof fields for final review; no provider home or credential is
added. `server doctor` separately reports whether the running private control
protocol offers provider readiness.

The central Git checkout and its repository-scoped deploy key are independent of
the provider login. A Git key grants repository transport; a provider login
grants provider execution; an RCP member token grants product authority. None is
accepted in place of another.

Remote execution adds one more transport boundary: the server's `rcp` account
must already be able to authenticate with ordinary OpenSSH to the exact account
in the selected machine profile. A remote project manifest records that account
as `machines[].os_account`; omission is refused rather than guessed. A
server-local team profile executes as `rcp`, including for older manifests that
predate the explicit field. RCP checks and uses that configured route; it
does not import a member's SSH key, collect one through Web/desktop state, or try
a different login. This SSH transport authentication does not select the remote
provider identity—the remote operating-system account and its native provider
state do.

There is no team fallback to a member laptop, personal checkout, local member
provider login, or different SSH account. Unreachable SSH, missing provider
authentication, or incompatible provider readiness fails visibly on the chosen
machine. Runtime-specific behavior remains behind the provider-call abstraction,
so Codex exec, Codex app-server, and Claude use their own provider contracts
without changing this local/SSH identity rule.

Remote canonical-state locking and publication are specified in
[Graph, history, and transitions](graph-history-and-transitions.md#canonical-publication).

## Seed and Refresh ingestion

Seed and Refresh alone ingest provider conversation logs. RCP supplies the
execution-host agent with configured provider log roots, project
`last_refresh_at`, the complete current graph and research rendering, exact
repository pointers, selected packages, and an optional human request.

The agent reads logs in place. RCP performs only bounded existence/readability
preflight and reports exact failures without blocking launch. RCP does not parse,
index, normalize, slice, hash, cache, transfer, or project provider conversation
content and maintains no per-log cursor or coverage truth.

That is the implemented ordinary-run path, not a promise to abandon source
history during a pending personal-to-team transfer. The confirmed transfer
target exports every complete provider conversation selected through the
existing native conversation index into the read-only project app-data root
`<RCP_DATA_DIR>/project-sources/<project-id>/provider-history/<provider>/`.
Configured provider profiles supply the native roots, while the index remains
the one owner of project-path matching and local/SSH source retrieval. Matching
is automatic and best-effort from recorded working paths; skipped sources are a
non-blocking diagnostic, not a transfer decision. The target agent reads those
imported provider-native sources alongside live roots from its configured
execution account. Local execution reads the project-owned source directly;
remote execution stages only that validated project-owned inventory as immutable
task input and leaves the remote account's live native roots in place. Resume
reuses the same staged fingerprint, and a missing or changed stage fails visibly
through clean retry rather than omitting history. Missing or corrupt durable
imported bytes block Seed/Refresh; the non-blocking best-effort rule applies only
to source-side selection before transfer. RCP-owned project chats
transfer only as human-visible history and remain excluded from Seed/Refresh
input under the native-chat-context rule. Imported sources never enter canonical
`.research`, a rebuildable cache, or the native provider home and never grant
Resume, Retry, credentials, or execution authority. The index copies the
selected original native transcript files byte-for-byte; RCP does not flatten
them into its lossy conversation-record model.

The watermark advances only after a Seed/Refresh Patch commits. Failed,
paused, interrupted, or rejected work leaves it unchanged. It is an
overlap-tolerant timestamp, not exactly-once ingestion.

Seed/Refresh correction stays scratch-only and offline over the retained staged
inputs. Large source-corpus fan-out is provider-owned and read-only; the root is
the sole Patch writer.

## Provider readiness and native skills

Readiness is an app-process service. Startup coalesces provider executable,
version, authentication, model-catalog, and configured machine probes; results
are cached for their configured lifetime. Explicit Refresh bypasses the cache.
Navigation never owns provider warmup and ordinary application use remains
available while it runs.

Provider-native skill inventory is app-scoped and separate from official RCP
packages. Startup refreshes each provider/machine target after readiness. A
successful refresh atomically replaces the last successful result; failure
retains it as visibly stale with a current diagnostic. Project open and launch
do not refresh it.

A native skill selection is structured per-turn metadata bound to provider,
machine, successful version, inventory hash, and name. It never changes launch
flags, authority, graph scope, or repository scope. A stale selection that the
provider rejects fails visibly without fallback.

## Official skills and workflows

Project Settings selects official packages. Every official skill is enabled by
default unless the human saves an explicit selection, including an explicit
empty selection. Official workflows remain opt-in.

Only selected packages are staged to the execution machine as immutable,
content-addressed directories. The agent receives compact id, version,
description, dependency, and exact pointer metadata; package bodies are never
embedded into launch prose. Slash completion inserts only the visible token and
a per-turn invocation pointer. Packages cannot widen surface capability.

The graph-authoring contract always includes the local causal check. Optional
`graph-audit`, `experiment-causality`, and `evidence-triage` packages provide
progressively deeper guidance. Requiring an executable graph scanner remains an
open question, not current behavior.

## Network behavior

Every user-facing Seed/Refresh, Discuss, Work, Experiment, Auto-research, merge,
and Paper surface retains its provider-native public-web behavior as defined by
its contract. Exact project write roots do not add a new network restriction.
Generic scratch-only Patch correction remains offline where its retained
contract requires that.

## Verification contracts

The durable observable boundaries are [S14 remote state](../acceptance/S14-remote-state.md),
[S15 real agent](../acceptance/S15-real-agent.md),
[S17 live preview](../acceptance/S17-real-agent-preview.md),
[S62 direct ingestion](../acceptance/S62-direct-provider-log-ingestion.md),
[S63 lock recovery](../acceptance/S63-agent-run-lock-recovery.md),
[S74 fail-closed containment](../acceptance/S74-boundary-inputs-fail-closed.md),
[S75 public web access](../acceptance/S75-network-access-on-every-agent-surface.md),
[S102 team execution](../acceptance/S102-team-runs-execute-as-the-space-account.md),
and [S119 stale-process exclusion](../acceptance/S119-stale-processes-cannot-command-the-next-turn.md).
