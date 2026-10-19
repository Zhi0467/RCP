# Providers and containment

This specification owns provider capabilities, launch construction, exact
project write scopes, remote execution, Seed/Refresh ingestion, provider
readiness, skills, and durable launch receipts. Semantic graph authority is
separate and defined in [Authority and Proposals](authority-and-proposals.md).

## Fixed task capabilities

Capabilities are fixed by each run owner; shared launch plumbing does not select
policy. [Authority and Proposals](authority-and-proposals.md) owns graph authority.

| Run | Operational contract |
| --- | --- |
| Seed/Refresh | Read provider logs and repositories; write scratch Patch only. |
| Discuss | Writable conversation scratch, read-only project context, public web; human chats may ask and query graph history. No active Patch contract. |
| Work | Noninteractive tools, public web, exact repository roots, optional Patch. [Consolidation](graph-consolidation-and-lessons.md) may commit keyed Patches during its turn. |
| Experiment-loop | Work-like access with focused Experiment and watcher authority. |
| Auto-research orchestrate | Work-like access, staged command client, orchestrator graph profile. |
| Episode merge | Scratch candidate; code landing additionally admits the episode worktree and shared checkout. |
| Correction | Retained scratch; Work-like repair retains the session and exact Work scope. |
| Artifact edit | Discuss scratch-only access on the admitted session, stage and version; no operational commands or master. Chat-master viewer comments use ordinary Discuss; composer comments use that chat turn's mode. |
| Paper coach | Read-only; no graph, draft-write or Apply channel. |

Manifest settings and skills select execution details and methods, not capability.

## Machine provider settings

The launcher reads each execution machine card's `provider_autocompact` at
launch using provider-owned validation and CLI rendering; unset values preserve
CLI defaults. `provider_shell_timeout` accepts whole minutes from 2 through 120,
with a default of 2 minutes for Claude, OpenCode and Codex. A turn with a staged
command mailbox resolves its configured shell timeout once at staging and carries
that value through every launch, including Patch corrections. Unset remains
unset even if the card changes later. Launches without a staged mailbox resolve
it once at launch.

Claude renders configured `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS`
into the `--settings` JSON's `env`, preserving other entries and overriding
user/project/local settings. The maximum is at least Claude's 600-second default.
OpenCode renders `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS` in its process
environment. All values are milliseconds; Codex renders no timeout variable.
Local and SSH launches use the same resolved value.

For live ask calls, the staged client and prompt use that shell timeout minus
30 seconds as the ask hold (90 seconds by default). The ask hold has its
own client argument and can exceed the ordinary command deadline; other verbs
keep their deadline. Remote mailbox checkpoints retain both the configured
timeout and the hold. Missing checkpoint fields use an unset timeout and the
default hold.

## Member terminals

`terminals/` owns member shells separately from provider tasks. Membership gates
the route. Shells run as the local service or remote execution account in a
registered checkout. They create no agent task, Patch or research receipt.

**Why:** Members are trusted on their project's host. A terminal removes the
agent intermediary from authority Work already gives them. Mount restrictions
resist mistakes, not deliberate use of the service account; real isolation would
need a separate identity, clone and credential broker. Revisit this boundary if
Work gains real isolation or membership includes people untrusted on the host.
Read-only access to a deploy key still conveys its transport authority.

Backend selection is per machine, independent of space and compute readiness:

- Linux requires `systemd-run`, `systemctl`, `findmnt`, a reachable user manager
  and `/bin/bash`, and reports `mirrored`. A degraded manager is usable. Linger
  is unnecessary for a PTY tied to its owning session.
- Other known operating systems use a plain PTY and report `cooperative`, with
  canonical-state protection explicitly unavailable. A failed Linux probe or
  mirrored launch never downgrades to cooperative.
- Remote selection probes the remote OS and account. A nonempty registered
  `os_account` must match both the cached probe and the launch connection.
  Failures distinguish unreachable, authentication, host-key and prerequisite
  errors. Every backend requires executable `/bin/bash` before admission.

The mirrored profile uses `systemd-run --user --pty`, `ProtectHome=tmpfs`,
`ProtectSystem=strict`, writable binds for the checkout and machine grants,
and read-only Git configuration/credentials not covered by grants. It shares
the host's `/tmp`. `protected_repository_paths` protects declared and resolved
`.research` paths, including read-only empty mounts for absent directories.
Overlapping or canonical-state repository roots are refused.

The launched profile must prove that each write root is writable and each
protected path exists, refuses writes and has a read-only mount (`findmnt`).
Only its readiness marker admits the PTY. Unsupported properties fail visibly.
Both backends use the repository cwd, `env -i`, and no startup scripts or saved
history; cooperative sessions perform no mount preflight.

systemd 254 or newer receives `--expand-environment=no`; older versions refuse
values containing `$`. Preflight also checks literal value preservation. D-Bus
transient units do not expand `%` specifiers, so paths are not percent-escaped.
Local and remote launchers share `terminals/profile.py`; remote Python source
is shipped, and canonicalization uses the remote stage's path resolver.

A completion marker distinguishes shell exit 255 from lost SSH. A lost session
ends rather than reconnecting to a replacement shell. Mirrored remote cleanup
confirms unit termination over a fresh connection, even after SSH exited.
Unconfirmed stops remain unfinished for startup reconciliation. Shutdown leaves
that record instead of waiting on the network.

An active Work turn in the checkout is a warning, not an admission block.
Git index locking handles ordinary collisions.

## Task-engine ownership

`BackgroundAgentTasks` owns common launch/runtime plumbing. Concrete owners keep
Auto-research, Experiment recovery, watcher admission and report policy. Follow
[AGENTS.md](../../AGENTS.md#cross-cutting-implementation-rules) for shared-code
boundaries. A change touching three or more engine entry points must move one
complete policy decision to its owner, not hide coupling behind a registry or bus.

An orchestrator-triggered chat is child Work only with a durable child-route row;
missing identity follows ordinary Work. The
[structural decision](../decisions/2026-08-20-backend-structural-refactor-closure.md)
records the accepted coupling.

## Provider runtime selection

Each project profile selects a provider-owned runtime. Defaults are Codex
`app-server`, Claude `stream-json`, and OpenCode `run-json`; Codex also offers
`exec`. A profile that names no runtime follows the default.
Readiness exports choices and the explicit default. Settings validates the pair,
profiles always resolve a runtime, and task requests cannot override it. One
profile's runtime covers all its capabilities, including Discuss and Work.

The current profile preference applies on every invocation, even when resuming
a native session. Tasks and Paper writing sessions record the actual runtime.
Codex app-server uses one fresh stdio process per turn, with shared local/SSH
launching, permissions, process control, usage and cleanup. Its persisted
threads may appear in Codex Desktop; RCP does not coordinate Desktop tasks.

App-server may use exec on the same machine and native thread only before the
new prompt could reach Codex. RCP records the actual runtime before the
`turn/start` write. Afterwards failure remains an interrupted or failed attempt.
A pre-prompt substitution records the passed-over runtime and diagnostic without
failing the human's turn. The capability, stage and scope do not change.

**Why:** Runtime is a profile preference, not permanent session identity.
Binding it to a session would prevent configuration changes taking effect on
resume. The prompt-delivery boundary prevents fallback from duplicating work.

App-server usage is the change in a thread's `tokenUsage.total` between its
pre-turn resume snapshot (zero when fresh) and active turn's final snapshot.
Snapshots replace totals, never sum them; other threads/turns contribute nothing.
A failed turn retains observed usage. Reports use `codex.app-server.turn.v2`;
older observations are not rewritten. `last` counts one model response only.

Both Codex runtimes support native subagents. App-server clears ambient custom
roles/instructions but retains delegation and parent permissions. Child notices
cannot answer or end the parent turn. Usage covers the parent only; RCP does not
read private transcripts to reconstruct descendant totals. Contracts require
native subagents to finish before the reply; RCP shows the subagents the parent
stream reports ([live status](#live-agent-status)) but never waits for them. Only helper/scheduler jobs outlive turns; RCP workers have their
own lifecycle. Claude launches set `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`,
keeping subagents foreground while allowing parallel Agent calls.

Codex exec `error` events are retry traces. `turn.failed` or nonzero exit fails
the turn; without `turn.failed`, the last retry error explains a nonzero exit.
The live decoder, remote fence and replay share this rule. A journal without a
terminal event cannot finalize or claim deliverable snapshots.

Containment differs by transport: exec disables user config and execpolicy
rules with `--ignore-user-config` and `--ignore-rules`. App-server supports
neither flag; RCP overrides capability-bearing keys but cannot disable `.rules`.

[Live steering](#live-human-steering) follows the actual runtime. App-server
uses `turn/steer` with `expectedTurnId`. Claude stream-json retains stdin and
uses `--replay-user-messages`; Claude decides whether a message joins the running
turn or becomes its next turn. Exec, including a pre-prompt fallback, cannot
receive input. The backend publishes each runtime's action label and refusal
reason.

## Cooperative project write containment

Project write scope protects cooperative users from accidental writes. It is
not same-account isolation, read secrecy, a general sandbox, network confinement
or resource supervision; see [design boundaries](../design.md#cross-cutting-invariants).

Selected-secret hiding is a separate read policy. After launch-host key
confirmation, RCP resolves one immutable `HiddenReadScope` before provider or
browser preparation. Prompts render its exact paths and effective status for
Discuss, Work, Experiment, orchestration, ingestion, correction, consolidation
and Paper.

Code defaults cover RCP databases/backups/checkpoints, provider credentials,
mailbox checkpoints, service keys, remote Claude tokens, private sockets and
selected WebKit storage. Private SSH identities and deploy keys are hidden only
after the right agent lists their decoded public-key SHA256 fingerprint and a
bounded signing check succeeds. Missing/stale public keys or failed confirmation
leave keys readable with a Settings/doctor reason. Public keys, SSH config and
known hosts stay readable; parents containing exempt keys cannot be masked.

Machine Settings accepts at most 64 absolute hidden folders. Host validation
canonicalizes and rejects overlap with checkouts, stages, tools, binaries,
command sockets and readable keys. Launch drops a newly conflicting saved folder
with `hidden_folder_conflict`, preserving code defaults. Any member may edit
these machine settings; they are not project manifest policy. Tool environments
drop case-insensitive credential-name globs and retain other variables. Provider
authentication stays outside the wrapper. Direct-tool credentials such as
`~/.config/gh`, `~/.netrc` and `~/.aws` are not default denies; Keychain stays open.

Deploy keys use `~/.rcp/ssh-agent/agent.sock`; user identities use the account's
`SSH_AUTH_SOCK`. The generic helper starts remote agents via systemd or launchd
when needed. A staged stdlib helper loads remote deploy keys and confirms both
agent types each launch. Provisioning/restore loads new local keys into a running
agent immediately. Git retains private-key transport and adds the stable agent
when available.

**Why:** This policy targets credential exfiltration prompted by generic untrusted
web content while retaining network, Git, SSH, schedulers, GPUs and browsers.
Research data, deliberate escapes through other process owners and targeted
RCP-content attacks are outside it. An environment allowlist would break lab,
proxy and compute variables; a denylist accepts unusually named secrets passing
through. Keychain and directly read tool credentials remain compatibility gaps.
Missing hiding support never blocks launch: Settings/doctor show the reason,
prompts state the effective scope, and no per-turn badge or trace is added.

RCP derives `ProjectWriteScope` from manifests, catalog, repository pointers,
lineage and run scope, never from client/provider requests. It binds project,
execution host/account, capability, exact stage/workspace, admitted aliases and
canonical roots, protected paths and a stable fingerprint. Prompts render it.

Construction requires a complete valid manifest inventory for every registered
project. Local manifests come from the opened workspace or registration; remote
ones use validated canonical mirrors, without a new catalog-wide connectivity
probe. Relative paths resolve against registered project roots. Snapshot locks
prevent reading provisional publication/rollback state. Missing inventory fails
closed.

The workspace must be inside its exact stage. Roots must belong to the same
project/alias/machine/host, exist and be writable there. Deduplication never
replaces roots with a common parent. Host identity comes from catalog/manifest;
a repository pointer's host describes how the agent reaches it, not ownership.
Its machine and canonical path must match on the execution host.

Repository roots cannot be another project's checkout, broad parent/home/temp
folder, app data, SQLite or canonical `.research`. Provider auth/session/cache
exceptions are runtime storage, not general project write roots.

### Watcher commands

`watch.json` check and cancel commands run as written in a fresh `bash -lic`
as the execution account, locally or over SSH. They are outside provider sandbox
and turn write scope, bounded only by timeout and process-group termination.
Human Cancel executes saved agent-authored text with the same account reach.

**Why:** Watchers outlive provider turns and need the observed job's login
environment, scheduler, network and sockets. A provider sandbox cannot supply
that contract. This cooperative boundary permits side effects beyond the turn's
roots and lifetime; hostile-agent containment would require a different owner.

### Machine writable paths

Repository-writing scopes receive machine-card `granted_roots`, plus `/tmp` and
the local temporary directory. Scratch-only/read-only launches receive none.
Humans choose paths; code fixes eligible capabilities.

Only RCP storage constrains grants: data and installed-server folders, credentials,
checkpoints/restores, `/etc/rcp`, `/run/rcp`, account `~/.rcp`, remote
`~/.local/share/rcp`, registered `.research`, launch inputs and legacy stages.
`rcp_owned_paths` and scope construction compare filesystem identity on the host.
Save and picker refuse roots equal to or inside protected storage. Storage inside
a grant remains read-only via `granted_protected_paths`, except the launch's own
workspace/jobs. Grants stay outside the fingerprint so the next chat turn can
use changed machine settings without losing resumability.

systemd uses `ReadWritePaths`/`ReadOnlyPaths`; Codex permits a deeper write root
inside a read parent. Claude denies override allows, so a protected parent of its
own stage is left undenied. A grant covering it can expose sibling stages to file
writes; Claude's shell is already unbounded.

**Why:** Configuring roots is distinct from granting a capability. Home, SSH and
other repository paths may be explicitly granted; only RCP storage is reserved.
The Claude exception preserves usable stages without a second file-edit hook.
A separate systemd write sandbox around Claude would alter command ancestry,
Stop and recovery while still allowing escapes through compute owners; it is
not the chosen write boundary.

RCP files live under private `~/.rcp`. Long-home socket paths may use a protected
`/tmp/rcp-<id>` directory; saved legacy `/tmp/rcp-run.*` stages remain protected
and resumable. Creation uses mode 700; existing owned directories lose group/other
write access, and directories owned by another account are refused.

## Provider enforcement

Selected-secret policy is rendered from `HiddenReadScope` on every capability:
Claude receives the staged shell prefix plus native read-path denies; Codex
receives file, directory, and glob denies in both exec and app-server policies,
plus the environment deny list; OpenCode receives the shell wrapper and native
read, grep, glob, and list denies where its rules can express them. An uncovered
native tool makes the launch visibly unhidden. macOS shell calls use a selected
path Seatbelt deny policy; Linux uses `bwrap` without a network namespace.
Hidden globs vary only their last path component. On Linux the wrapper empties
each glob's parent and binds its other existing entries back, so a match created
after a shell or browser daemon starts, such as a WAL file or a backup, stays
hidden. A hidden literal that does not exist yet is hidden the same way under its
existing parent. A shell command never empties the account home or a folder
above that parent, so deeper misses are rechecked at the next command; the
persistent browser daemon also masks at the home, so a credential folder created
mid-session never appears to it. A changed mask set restarts the daemon at its
next admission. The wrapper and its policy live in `~/.rcp/hidden-read/<fingerprint>`
on the execution host, outside every write root and inside the hidden set. A
launch already resolved unhidden runs its commands silently; if hiding was
enforced at launch and the sandbox later becomes unavailable, the wrapper
refuses the command rather than run it unhidden.

A durable chat or episode worktree binding replaces exactly one registered alias's
root with its validated worktree root on the same execution machine and host.
Catalog ownership and overlap checks validate the registered checkout and
the planned sibling before creation, and the replacement before launch. The
binding also pins Git's canonical common metadata directory: it is admitted as
an exact metadata write root so Git can update the branch and index without
admitting shared checkout files. The scope prompt renders this same root. The shared checkout is excluded from ordinary Work scope;
Discuss receives the same worktree pointer as read context only. A
human-selected local integration turn alone admits both exact roots. Its target
is backend-resolved and persisted, never taken from a client-supplied path.

Episode Work and orchestrate use the owner's binding. They never receive the
shared checkout as a repository write root. Each launch refuses a machine grant
covering that checkout. A default temporary grant that covers it is omitted.
Repository context renders the same bound worktree used by enforcement.

### Codex

Work and orchestrate use Codex's native noninteractive project permission
profile: the exact workspace plus admitted repository roots, never the dangerous
bypass flag. Fresh launches set the exact working directory; native resume uses
the provider's supported configuration path while preserving the same scope.
Public web search remains enabled. Canonical `.research` nested in a writable
root is refused as a write and kept readable, because RCP stages its `graph.json`
and `research.md` as required run context; Codex treats a denied path as
unreadable rather than unwritable.

Work, orchestrate, and Discuss also deny reads of the desktop app's WebKit,
application support, cache, HTTP storage, and cookie paths for both release and Dev
bundle identifiers. These rules are separate from protected write paths.
Home-relative paths resolve on the execution host. Discuss uses a named native
permission profile so the same denial applies in exec and app-server; the
app-server checks the activated profile before starting a turn.

### Browser grants

Browser is a per-chat human grant, off by default, resolved immutably at admission
and passed through launch construction. Work, orchestration and Discuss may use
it; Paper, ingestion, scratch correction and recorded replay may not. Prompts
name the already-open session and invocation directory, starting with `goto`.
`open`, `close`, `close-all` and `kill-all` end that session for the turn;
`delete-data` also removes logins. Continuations send explicit off/unavailable.

The CLI tool directory prefixes PATH; SSH uses the host's PATH after login-shell
initialization, never the controller's. Codex retains `shell_environment_policy={}`.
Owners release grants even on prompt failures. Detached remote work retains its
lease until settlement proves the provider stopped; restart restores routing and
adopts pending leases. Settlement records browser status before deferred cleanup,
without acquiring again. [Host runtime](#host-browser-runtime) owns lease cleanup.

**Why:** A headless Playwright CLI daemon preserves tabs, pages and logins between
turns on the execution host, including access to its local services. It avoids
another MCP channel and repeated tool descriptions. Every CLI feature, including
`run-code`, remains available by human consent.

The daemon runs outside provider write containment. It can write anywhere and
reach any address its account can; team agents can address each other's sessions.
The grant accepts those gaps without changing graph authority. Selected-secret
hiding has the platform limits below. RCP installs the CLI per host; Node 20+
and Linux browser libraries are prerequisites. Launches offer no MCP server or
MCP configuration UI.

### Claude

Work and orchestrate use Claude's supported unattended `dontAsk` mode with an
RCP-authored strict settings allow-list for the exact workspace and admitted
repository roots. They never use `bypassPermissions`. RCP suppresses user
settings for this enforced launch. Every Claude launch passes strict empty MCP
configuration; supported skills remain available. Discuss with a broker gate adds
only `Bash(python3 -I -S <staged-client-path>:*)` for that gate's exact staged client.
Its Edit denies protect the client directory itself and all descendants, including
when an additional read directory or a legacy workspace contains it. The literal
client directory is escaped for gitignore matching; intentional hidden-read globs
keep their glob semantics. These denies merge with the launch's hidden-read
settings. A browser grant separately adds
`Bash(playwright-cli:*)`. Public WebSearch and WebFetch remain available under
the provider contract. Every other launch (Discuss, Seed and Refresh, the paper
coach) receives its read context as `Read`, `Glob`, and `Grep` allow rules per
directory, never `--add-dir`: under `acceptEdits` an added directory is also
editable, which would hand Discuss write access to project files.

**Why:** Claude's OS sandbox is off because its Linux backend always unshares the network
namespace and remounts a minimal `/dev`, and no setting relaxes either, so a
sandboxed Work turn cannot reach a scheduler, a GPU device, its own command
broker, or any non-HTTP service on its execution host. Turning it off is a
deliberate capability choice: Work on this provider is meant to run real
compute, and a containment that forbids that is not usable containment.

Write roots are therefore enforced by Claude's file permission rules. `Edit(path)`
is the only rule kind Claude matches for file writes, and it covers every
file-editing tool; a `Write(path)` rule is accepted and then ignored, so RCP
emits only the `Edit` form. These rules govern file-editing tools, subject to the
[grant exception](#machine-writable-paths), and do not bound `Bash`. A Work turn's shell can therefore write outside its admitted roots
on its execution machine. That is an accepted accidental-write gap for this
provider, not a claim of containment; Codex's native permission profile still
bounds both. Nothing here widens graph authority, which stays with `patch.json`.

### OpenCode

Every OpenCode launch carries its rules in `OPENCODE_CONFIG_CONTENT`, which
OpenCode merges last, and sets `OPENCODE_DISABLE_PROJECT_CONFIG`. The rules
belong to an RCP agent that the launch selects with `--agent`. OpenCode applies
agent rules after top-level ones, so neither the user's global rules nor their
own agents can widen them. The agent is named after a digest of its rules, so no
user agent can share the name and be merged in. Every launch also passes
`--pure`, because a plugin's config hook could rewrite the rules.

The rules deny every tool first, then allow only OpenCode's built-in read-only
tools, then add each capability's edit and shell rules. So OpenCode subagents
(`task`) are unavailable, since every subagent, built-in ones included, runs
under its own rules. So are MCP and custom tools from the member's global
config. Every launch also turns off formatters and language servers, which run
their own commands on edited files outside any tool rule.

Edit rules deny everything, then allow the exact workspace and admitted roots,
then deny protected paths, then allow again each root inside a protected path,
such as a stage inside RCP storage; OpenCode applies the last rule that matches.
The paper coach and every other capability may read outside their folder. OpenCode
matches them against paths relative to its project root, which is the enclosing
Git work tree, or `/` outside one. RCP writes them for `/`. A launch that carries
path rules therefore refuses to start inside a Git work tree, rather than let
the rules name the wrong paths. Task stages are outside Git. The paper coach runs
inside the project and denies every edit and the shell outright, so it needs no
path rules. Discuss and ingestion may edit only their workspace and their own
write folders. Ingestion has no shell. Discuss denies `*`, allowing only
`python3 -I -S <staged-client-path> *` when its broker gate names that client, and
`playwright-cli *` when the human granted the browser. Final edit denies protect the
client directory itself and all descendants in split and legacy stages.
Without a broker gate, including artifact edits, there is no client allowance.
Command selection and launch configuration derive the same rules, including on resumed sessions.
Codex Discuss retains its existing `rcp_discuss` permission profile.

Every staged command client, including Work's, and its broker wrapper use Python
`-I -S`: no script-directory or `PYTHONPATH` imports, user-site imports, or site
initialization.
The prompt and exact-client shell grants derive from that same executable argv.

Work keeps the shell. Like Claude's rules, these bound every file-editing tool
and do not bound the shell, so the same accepted accidental-write gap applies.
A rule that would ask is rejected, because `opencode run` cannot ask.

OpenCode prints no event when a turn ends, and a step that stops can be followed
by another. The launch wrapper therefore prints one `rcp.provider_exit` line
after the process exits; that line ends the turn locally and in the remote
fence. Every text part is part of the reply, as Codex's agent messages are.

### Version failure

Provider profiles own the minimum supported CLI contract. If the installed
supported version cannot enforce exact unattended roots, the launch fails with
a provider-compatibility diagnostic. A selected Codex app-server version or
startup that fails before prompt delivery may use the explicit exec fallback;
exec must still enforce the same capability and exact roots. RCP never restores
broad bypass access or treats prompt wording as containment.

### Adding a provider

A provider is one package registered as the `rcp.providers` docstring
describes. The shared defaults were written for Codex and Claude, so a new
profile decides each of these explicitly rather than inheriting them:

1. Session files: a session format and roots, or none, which leaves RCP not
   indexing its history.
2. Turn end: the event its fence reads, or a wrapper line when the CLI prints
   none. Both the local decoder and the remote fence must end on it.
3. Login: an `auth.py` when RCP manages sign-in. Without one, Provider logins
   shows a note with the CLI's own login command.
4. Models: probed efforts may be empty, and every stored reasoning field must
   accept that.
5. Version floor: the CLI version whose containment was probed, applied to every
   capability that relies on it.

Containment is proved against the real CLI at that floor: config precedence,
path bases, and anything that can rewrite the rules, such as plugins. Tests that
cover every provider iterate `PROVIDER_IDS`; only tests of one provider's
protocol name it. Before merge, drive the served app through Discuss, Work,
Provider logins, and the model picker, locally and on an SSH execution machine.

## Continuation binding

Launch receipts record project, host, capability, canonical write roots,
fingerprint, enforcement mode and fresh/resumed state. Their canonical-state
boundary is `provider_enforced` only where every tool is bounded (Codex) and
`file_tools_only` where the Work shell is not (Claude, OpenCode). Before any
resumed launch, RCP recomputes and compares project, host, exact stage, graph
target and root fingerprint. Missing/moved roots, cross-project bindings or incompatible
run scope fail before launch; they require a fresh session.

The session id actually passed to the provider decides whether this is a
continuation, not admission's label. A sessionless launch establishes a new
stage binding, but refuses an overlapping live turn with another scope. A
continuation compares the current stage binding, not one superseded by a fresh
launch. Refusals name both repository sets and direct run-scope changes to a
new episode.

Conversation-local merge and the following ordinary turn may transition between
exactly recomputed worktree-only and worktree-plus-shared roots for the same
binding. Resume/Retry of an already-bound operation still requires its original
fingerprint. This exception cannot change project, host, stage or repository.

The same resolver covers Work, orchestration, children, wakes and corrections.
Runtime fallback preserves every binding and the native session. Recovery keeps
the original assignment and completed progress. Current inputs, schema,
validator, output and command metadata travel inline; they do not authorize
repeating external effects. After Work-like correction, RCP revalidates the
candidate against current state even if its bytes are unchanged.

## Continuation prompts

A launch without a native session sends the owner's full contract and records
its exact bytes as the session master. Handoff, provider switch, clean Retry,
progress handoff and fresh watcher notification are session starts.

A continuation sends its reason, new human input/skills/trigger/diagnostics,
current restrictions, and stable values that differ from the master. Paths,
command clients, write roots, repositories, skills and execution facts use this
delta. Current values override the master; omitted values retain it. Graph head
uses the owner's seen head instead. Attachments are plain listings. Watcher
maintenance corrections restate their resource's current rules.

One final pointer identifies the original contract and asks for a read only
after compaction or lost understanding. RCP restores the exact master bytes to
the stage before each pointer, selected by native session id even for owners
with per-turn stages. A missing stage fails closed. Only a succeeded operation's
master counts; absent records or changed master keys bootstrap a replacement.
The key includes shared master version, graph-rules digest, owner policy version
and ontology-extension mode. Branch-merge continuation also resends graph rules
when main's extensions differ.

**Why:** A stable pointer recovers compacted context without repeating a full
contract each wake. Durable bytes prevent dangling pointers; successful delivery
prevents pointing to instructions a failed launch may never have sent. Current
authority stays inline so a master cannot restore expired commands or permissions.

Discuss, Work and Experiment share the artifact contract rendered from the media
model and enforced caps, including live-data fields. Empty output is normal;
a commented writable file is edited in place. The `live-pages` package supplies
examples; only episodes offer episode needs. Existing sessions keep their master
until its version key changes.

A report is a separate prompt node: it revokes operational instructions and
carries no master pointer. The next operational launch explicitly reopens its
master until an operational attempt succeeds. Each owner builds its parts;
`agents/continuation_prompt.py` composes them and `runs/session_master.py` owns
the durable record.

## Graph history queries

Every graph-reading run except episode reports and origin-bound artifact edits
can ask RCP how the graph changed, through two read-only command verbs:
`graph_diff <head A> <head B> [ids]` (or `--branch-changes <branch>`) and
`graph_log <id> [--at <head>]`. Heads use the target-qualified grammar in
[Graph, history, and transitions](graph-history-and-transitions.md#history-reads).
That covers Discuss, Work, Experiment loops, the Auto-research orchestrator and
its children, branch merge, Seed and Refresh, the paper
coach, and nightly consolidation. A run may read main and every branch of its
project, except that a run on a fork reads only its
[ancestry](auto-research-and-branch-merge.md#forks). The runs that
[group read scope](#group-read-scope) names also read every project in their
group; writes stay pinned to the run's one target.

Each change in an answer gives the entity id, type, title, change kind, and
every changed field's before and after value, never unchanged fields. Each step
gives its head, producer, task id, episode id, and authored summary; inherited
main steps on a branch log are marked inherited. A log lists its steps newest
first, like `git log`. Answers are bounded by count
and encoded size in `limits.py` and continue through a cursor pinned to the
resolved heads, so a moving `@head` never shifts between pages. A long value
continues on the next page rather than being cut. Neither verb is a mutating
verb, writes a file, or contributes graph authority (invariant 4b).

Each owner resolves one read scope at launch: project, readable targets, and the
allowed verb set. The command handler checks every request and cursor against
that same object, and the prompt renders it. Discuss gains these read verbs
alongside its human-question channel; a report or artifact
edit keeps its command revocation. One prompt helper renders the query section
from the launch's read scope; a session master or shared prompt finalizer that
receives a scope appends it, so an owner passes its scope rather than writing
the section itself.

**Seen head.** RCP records, per conversation or episode lineage, provider
session, and graph target, the head of the last graph snapshot a turn received.
A turn's own Apply advances it only when no other accepted step landed between
the snapshot and that Apply, so a teammate's mid-turn edit is never hidden. It
is written when the turn commits, never at prompt construction, on a failed
attempt, or because the agent queried a later head. A continuation whose staged
head differs lists `current.graph_changed_since: <seen head>`; an unknown seen
head lists nothing and is not "unchanged". The master states once how to query
that range. Seed and Refresh get their refresh range the same way, and a
consolidation run gets its covered head, which stays separate from any seen
head.

### Group read scope

When a run's project belongs to a [project group](projects-spaces-and-operations.md#project-groups),
every project in the group shares one read scope. Discuss, Work, Experiment
loops, and the Auto-research orchestrator and its children receive it. Seed and
Refresh, the paper coach, branch merge, reports, consolidation, and artifact
edits keep their own project only. The scope names the run's own project, which
unqualified heads still resolve to, and the readable group projects; `graph_diff`
and `graph_log` take an optional project id and read any target of a project in
that set.

The prompt renders the same object as a group section: the group, which project
is the run's own, and for each other project its canonical `graph.json` and
`research.md` paths and its repository pointers, with an SSH host when they sit
on another machine. These are pointers to live files, never staged copies.
Each of these runs renders the section every turn, and its master values keep
the group name and readable project ids, so a continued chat or episode learns
when its project leaves the group. A Patch correction reuses its launch's group.
Sibling repositories never become write roots, admitted repositories, run truth
scope, or Patch repository declarations, and a Patch still validates against
the run's own graph only (invariant 4b).

## Graph rules in task contracts

Every contract that reads or writes the research graph includes one rendered
graph-rules block, and no contract restates field or relation meaning in its own
prose. The block renders from code: node field descriptions, base relation
descriptions and authorable endpoints from `RELATION_SPEC`, which relations
carry an Evidence assessment, id prefixes, and value vocabularies. Retired
relations remain available to historical readers but are not offered as new
links; the authoring choices and validation use the same specs. A read-only
contract (Discuss, chat) receives the definitions and the method for searching
`graph.json`. A graph-writing contract also receives the hand-written method
that spans several nodes: the Experiment input-versus-result causal check and a
short list of cross-cutting write habits. Ontology extension rules are added
where the project has extensions.

Authority is not part of the block. Each contract places its own authority
block beside it: the ordinary agent contract, the orchestrator profile, or the
Experiment-loop allowlist. A skill may teach method but does not restate the
block's definitions.

The block carries a version digest over its rendered text. It lives in the
session master, so continuations, wakes, corrections, and added turns do not
repeat it. Every master key includes the rules version, so changed rules reach
an existing session as a replacement master on its next continuation. A
human-started graph repair renders the current contract rather than relying on
the one its session began with.

## One graph output channel

The collection boundary is [invariant 4b](../../AGENTS.md#stable-invariants).
Conversation graph changes collect only `patch.json` from their exact task stage.
Ordinary Work also collects `watch.json` and the required `outcome.json` there;
the latter reports the turn outcome and grants no graph authority.
Ingestion and branch merge prefer it, then accept exactly one patch-shaped JSON
file if it is missing or not Patch-shaped. They inspect at most eight files,
`patch.json` first then filename order; multiple candidates are refused.
Local and remote collection read only regular files and never follow a link.
The labelled final assistant message is the answer, separate from traces,
reasoning, tool output and Patch verdict. Rejection preserves an existing answer.

Seed, Refresh and Work stages include a Python command client. Bounded mailbox
requests let RCP prepare candidates against live state and record validation.
Exit codes distinguish valid, semantically invalid and unavailable. Apply
re-prepares bookkeeping and validates under the canonical append lock; a prior
check reserves nothing. Operations retain written order, with whole-Patch lookup
for legal forward references.

Commands return JSON within `COMMAND_CLIENT_WAIT_SECONDS`. Transport `delivery`
is `not_sent` only when RCP could not receive the call, otherwise `unknown`.
Handler fields such as `submitted` and `disposition` are separate. A keyed file
command sends its checked filename; first admission reads it, while repeats use
the recorded result even after Apply consumed the file. Mailbox/broker refusals
are bounded task events. Broker notices are signed under a notice-only prefix
and bound to their filename.

`ask --key K --question TEXT` accepts repeated `--choice` and optional `--multiple`
(which requires choices). It binds immutable origin and arguments; identical
keys reuse a question, changed arguments fail. Its result carries `question_id`
and `pending`, `answered`, `dismissed` or `parked`; answered includes text and
choices. Fresh transport ids poll the same key under one deadline. Once any round
delivered, later transport failure stays `unknown`. At deadline the agent may
repeat exactly or end its turn.

Human Discuss/Work chats, human Experiment episodes and the orchestrator may ask;
viewer edits, workers and child Work/Experiments may not. The handler's resolved
verbs also drive the prompt. For live answers, the client acknowledges the
server's durable receipt token through the same authenticated request path only
after parsing the complete response. Tokens stay out of agent stdout; diagnostics
go to stderr. Receipt requires acknowledgement and successful settlement.
Publishing bytes alone never suppresses follow-up; ambiguous delivery remains
eligible after restart. Owner delivery is in
[human input](conversations-episodes-and-watchers.md#conversation-scratch-and-human-input)
and [Auto-research](auto-research-and-branch-merge.md).

**Why:** The command channel already owns authentication, idempotency, events
and delivery. Provider ask tools differ or are unavailable in unattended modes;
a separate ask MCP server would duplicate those mechanisms. Answers are human
input, never authority to change scope, budget or protected beliefs.

Work and Experiment, including correction, bind their broker to the live provider
process tree on the execution host. Prior turns cannot gain new authority;
validate-only credentials refuse keyed commands. The helper's `launch` verb and
scheduler route are owned by [compute jobs](compute-jobs.md); `lesson` is owned by
[graph consolidation](graph-consolidation-and-lessons.md).

Remote Discuss, Work, Experiment and child Work mailboxes retain one thread owner
through settlement. Disconnected accepted turns transfer it to reconciliation.
Settlement fences new requests and drains admitted work; shutdown suspends the
owner with a private checkpoint. Startup restores the same id, token, budget
reservations and results before liveness checks. Restoration requires both a
saved `ask` offer and current authorization; old checkpoints cannot add it.
Work retains server-owned `lesson` authority. Credential-store protections and
backup exclusion apply to checkpoints.

Listing, reading, handling and publishing retry separately on typed temporary
transport failures, with bounded jittered backoff and one outage/recovery event.
Completed responses and validation reservations are reused across lost writes
and restart. Permanent failure or Stop publishes closure; failed drains are
bounded. An unreachable host may prevent that publication but proves no provider
death. Polling and retry bounds live in `limits.py`.

## Durable task lifecycle

Agent work belongs to the backend, not a browser view. Before execution, RCP
persists the task, task attempt, authorizer, capability, target, exact stage,
provider identity, and write-scope binding. Immediately before prompt delivery
it persists the actual provider runtime. Provider events retain labelled
answers, native session ids, usage, diagnostics, Patch results, and launch
receipts. A provider that succeeds while silently dropping part of the launch
records one backend-authored note on its exit receipt, and the task projection
exports that note to every surface listing or opening the task: the run reads as
untroubled otherwise. The note is composed from what RCP requested. Provider
diagnostic output is never shown to a human as the explanation.

A provider's final result is both its answer and its accounting boundary, and a
labelled answer is withheld from the wire, so its usage is forwarded on its own
frame. A turn that succeeds is counted exactly like one that fails, and a queued
follow-up's second result is counted once more.

### Live agent status

Provider profiles also decode a display-only `AgentStatusDelta` from their
stream: context tokens at the latest model call, the context window, compaction
start and result, and native subagent changes. The background task folds the
deltas into one `agent_status` snapshot on the task row with
`fold_agent_status`, writing at most once per `AGENT_STATUS_WRITE_SECONDS` and
at once for a compaction or subagent change; the task watermark carries it to
the chat. Claude reports context per assistant message and its window only in
the final result. Codex app-server reports both on every token-usage update,
compactions as `contextCompaction` items, and subagents as
`subAgentActivity` and `collabAgentToolCall` items. Codex exec reports none of these, so its chats show
no context. When the machine card's auto-compact value is a token count, the
launcher adds it to the snapshot.

**Why:** the snapshot is display only. It never enters a prompt, Patch, answer,
turn outcome, or usage accounting (invariant 4b), so a decoder gap degrades the
panel and nothing else.

Remote launch waits for the provider process group, including when its session
wrapper forks. A successful SSH exit or partial assistant message cannot complete
a Codex turn: its protocol must report completion, and the remote process group
must be confirmed stopped before a correction or recovery can reuse the stage.
Each remote pass has a unique pidfile and a durable start/stop receipt. An
unresolved pass fences stage reuse across task failure and server restart; a
read-only check may release that fence only after confirming process absence.
A host that reports no pidfile inside a stage that still stands has confirmed
that absence: the wrapper writes its pidfile before it execs anything and RCP
never removes one, so nothing was started. That is the only inference allowed,
and it is what keeps a launch that died before reaching the host from fencing
its conversation for good. It is drawn only once the launch has ended: the
post-exit confirmation and the pre-flight fence read absence that way, while a
stop racing a launch still in flight does not, because a pidfile that has not
appeared yet may only be late. A vanished stage, a stage path that now leads
somewhere else, or a directory at that path RCP would not adopt as a stage
(not the account's own, or not private) is not absence, because whatever
removed or replaced the stage could have taken a running pass's pidfile with
it. Unreachable hosts,
and pidfiles that exist but cannot be read, keep recovery blocked and preserve
the stage and receipts for reconciliation.

Pause, Resume, Retry, and correction form explicit parent/child attempt chains.
They retain task mode, graph target, capability, stage, and external-effect
diagnostics. Provider, model, and reasoning are one niche a human may change on
a recovery, because what makes a recovery worth starting is often that the
current one cannot finish the turn. A rebound recovery starts a clean native
session, since the prior continuation belongs to the binding it left, so the
rebinding reaches exactly the actors that have a clean-session path: an
Auto-research worker continues only through the exact session its dispatch bound
it to, and its recovery is refused rather than admitted as a turn that cannot
launch. The execution machine moves only where nothing is anchored to it. An
episode's watchers, stage, and children live on the machine its turn runs on, so
a recovery inside an episode keeps that machine on every Retry path, while a
standalone turn owns nothing there and may move to any reachable one. A failed run retains its scratch and Patch text for bounded
same-session repair and normal retention; RCP does not delete evidence merely
because validation or transport failed. Age-based cleanup first excludes exact
stages owned by active tasks, committed native chat sessions, live episodes,
pending or running report wrap-ups, and legacy candidate and source stages
awaiting background import. One
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

### Live human steering

Steering addresses RCP's live process for one exact ordinary Discuss/Work attempt.
It cannot select a newer attempt, start work or target an episode worker. Agent
mail uses its own channel. Input retains the attempt's mode, scope, target and
budget; [chat records](conversations-episodes-and-watchers.md#conversation-scratch-and-human-input)
own its human message.

Receipts are **delivered** (provider acknowledgement), **refused** (with reason)
or **unknown** (write may have begun without acknowledgement). Codex uses the
matching `turn/steer` response; completion fences new input but drains matching
responses before shutdown marks unresolved input unknown.

Claude becomes ready at `command_lifecycle state=started` for RCP's initial UUID.
A follow-up's `queued` acknowledges delivery; replayed user echoes do not. RCP
tracks only UUIDs it generated: `queued`/`started` add outstanding commands,
`completed` removes them, and each result removes its attributed
`user_message_uuids` or `user_message_uuid`. An outstanding accepted follow-up
keeps the process open for another result. Otherwise RCP ends it and refuses
unacknowledged follow-ups. Missing usable UUIDs fail closed to that stop rule;
`queued_turn_count` is ignored. Successful `task-notification` results with no
RCP message id remain traces in both live decoder and host fence.

Claude decides whether input joins the running turn or becomes its next one.
Both share session, stage, capability and scope. Answers join with a blank line
into one chat reply; each result counts usage separately. Work reads Patch once
at invocation end and clears scratch only on entry. No extra task or answer file
is created.

A dropped connection after writing leaves unknown delivery, except at Claude's
explicit result fence. `PROVIDER_STEER_ACK_TIMEOUT_SECONDS` bounds receipt wait,
not the turn. Late responses do not rewrite outcomes. Durable reservation occurs
before the external write; restart leaves unacknowledged reservations unknown.
RCP never retries, resends or queues a refused steer. SSH uses the existing stdin
pipe. Pause, Retry, Stop and restart retain their normal lifecycle; replacement
processes do not recover in-flight steering.

## Local and SSH execution

Providers run on the selected repository execution machine with the same
capability and scope locally and over SSH. Remote launches use remote canonical
roots. The remote stage owns validation, wrappers, transfers and recovery;
resumption proves its saved host and stage. Transport failure is unavailable,
not semantic correction.

SSH connections are owned per durable unit of work: stage or lock. Short control
traffic shares a connection. Cleanup uses that shared route, not the failed
work's route, so OpenSSH can reopen it. Finished-work sockets are removed only
after probing liveness; another RCP instance may own them in the account's shared
socket directory. This limits connection coupling, not host-wide network outages.

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
discard the v3 field during migration.

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

The backend owns team provider calls. Local runs use the service account;
remote runs use the exact profile's configured SSH account and its provider
state. No account switching or alternate provider homes are created.

Provider profiles own sign-in interactions, validation, environment, protocol,
safe metadata and evidence interpretation. The credential store owns private
atomic persistence and collision-free paths; remote keys digest the exact host.
Ambiguous legacy remote token paths require repasting, never guessed migration.

Codex device sign-in uses app-server `account/login/start` with
`chatgptDeviceCode`, the reply's URL/code, completion's `success` and
`account/login/cancel`. It parses no console prose; missing completion records
failure. Codex also owns logout and verification. Claude validates/stores a
setup-token, estimates lifetime, injects `CLAUDE_CODE_OAUTH_TOKEN` into every
process and removes conflicting inherited credentials. Tokens travel through
stdin/private environment, never argv, events, responses, receipts, logs or
backups. The `providers` store is backup-excluded. Required missing credentials
refuse admission even without a login row.

**Why:** Startup serialization reduces rotating-token races; Claude's static
setup-token avoids rotation entirely. A shared persistent Codex process would
need per-thread permissions and command authority independent of process ancestry,
plus remote attachment and interrupt semantics. Until those exist, turns retain
separate processes and unnecessary probes are avoided. Codex mid-turn refresh
races remain possible; verified sign-in is the recovery.

The account lifecycle owner serializes sign-in, verify and sign-out under the
gate through durable publication. Concurrent device sign-ins join one operation.
Replacement first publishes a signed-out fence, so interruption cannot admit an
unverified credential. A minimal real request must succeed before sign-in counts;
provider status only proves credential presence. Successful verification
invalidates readiness and releases parked work through existing owners, once.
Completion and restart reconciliation belong to the service, not HTTP polling.
Any authorized member may sign in/out; durable actions record the member.

Routes under `/api/providers/{provider}/logins` provide `sign-in`, status
`sign-in/{login_id}`, `token`, `verify` and `sign-out`. Unsupported interactions
refuse explicitly; status GET is read-only. Sign-out records a new generation,
`source="sign_out"` and actor and fences admission.

### Login state and failure

One durable `(provider, host)` login row stores `signed_in`/`signed_out` and a
generation. Provider-owned revoked-login evidence classifies `provider_auth`
across turns, wakes, workers, reports, recovery and probes. Only the generation
captured at launch can revoke the account; late failures cannot revoke a repaired
login. Claude recognizes its remote missing-token fence and declares no unobserved
native revocation signatures.

Signed-out or missing-required-credential accounts refuse paid admission before
task creation/budget debit. Queued tasks remain queued, reconcilers retain their
inputs, and auth recoveries remain blocked. Readiness, Retry and server checks
read that same state. Verify runs a minimal authenticated request under the gate.
Success increments generation, invalidates readiness and releases account-bound
recovery, queued work, mail, lifecycle/watcher inputs, reports and eligible
Experiment Retry once. Ordinary episode reconciliation retains other accounts'
fences. Authentication failures sign out; transport/configuration/ordinary failures
do not. Errors direct members to Settings, Provider logins, without exposing
credentials or prescribing shell login commands.

Provider discovery uses PATH, the OS account's `~/.local/bin`, then profile-owned
native paths, locally or in the remote interactive login shell. An explicit path
fails as given, without choosing another installation. Successful absolute paths
preserve symlinks for native updates; home is the OS home, not inherited `HOME`.

`rcp server provider check` requires exactly one `--request` or `--project`.
It checks the configured executable, version, login, runtime, model and effort
through the normal launch abstraction, not arbitrary host/account overrides.
The running service owns probing through its private control socket; the CLI does
not open its SQLite. A SHA-256 boundary over request revision or project profiles
is rechecked before subprocesses. Request results retain only executable,
bounded version, runtime, observed account and check time; later checks reuse
that path. Doctor reports readiness-protocol support. Implementation errors stay
errors, not invented missing-install diagnoses.

Team admission uses `effective_repositories`, including completed Add/Connect
provenance. Invalid provenance refuses admission. Server-only repositories need
no deploy key or `core.sshCommand`; GitHub-backed ones retain key checks. Missing
keys refuse terminals or produce the provider transport notice. Provider login,
Git transport, SSH access and RCP membership are distinct authorities.

Each GitHub-backed checkout pins its own key with `core.sshCommand`, written by
provisioning and backfilled by verification. Every launch, including Discuss,
carries that transport. Repository writes still follow capability, but Discuss
can push existing refs or delete remote branches. `GIT_CONFIG_SYSTEM` supplies
member display name and `<member-id>@members.rcp.invalid` at lowest precedence;
shared repository/global config may override it. RCP stores no personal Git keys
or settings. Authorship is unverified; pushes authenticate as the deploy key.

**Why:** Shared-account personal keys cannot provide per-member isolation. Each
checkout needs its own key because one launch may use several repositories.
Membership therefore grants repository transport write access; removing membership
revokes product access. Discuss keeps no repository-write allowance despite its
transport access.

Remote machines require explicit `machines[].os_account` and working ordinary
SSH from the service account. Server-local profiles use the service account.
RCP imports no member SSH key and never falls back to a laptop, personal checkout,
provider login or alternate account. Unreachable or unready targets fail visibly.
[Canonical publication](graph-history-and-transitions.md#canonical-publication)
owns remote state locking.

### A remote turn outliving its connection

Ordinary Work, Discuss, Experiment-loop and Auto-research child Work register
typed, durable finalizers. Routing uses the retained contract, never chat kind.
Zero or multiple contracts leave the task visibly waiting. Other owners retain
their own launch behavior. Local turns register no remote finalizer.

Each owner settles its own effects: Work's Patch/watchers/reply; Discuss's reply
and session binding; the child's parent-authorized result; Experiment's joint
Patch/watch admission and wake binding. Experiment records its baseline, control
snapshot and committed wake session before launch. Recovery creates no second
child or invocation and retains the original authority, target and accounting.

Supervised Work corrections retain the primary answer in an immutable checkpoint.
Replay preserves it, even when correction failed, and starts no further correction.
A recorded deliverable needing correction is rejected; manual graph repair keeps
its separate launch path.

The host supervisor is shipped as a staged source file; launches without that
path are refused. It journals output and records prompt acceptance before any
prompt byte reaches the provider. Host acceptance, not a controller receipt,
proves work may have begun. The supervisor detects turn end but assigns no
semantic verdict; the live decoder also reads recorded bytes.

Recorded decoding uses the owner's original capability and retained Work write
scope. Codex app-server rechecks the actual journalled thread response against
that contract, including its active permission profile; recovery does not
substitute a read-only capability or waive enforcement.

At turn end it snapshots Patch, watcher handoff and maintenance files under
digests. Before reading deliverables, finalizers restore that exact set, removing
later maintenance files. Per-file and aggregate limits apply: overflow makes the
whole set incomplete. Journals without snapshot metadata leave files alone.
Every supervised launch also records its pinned native session, including each
correction. A mismatched recorded session is refused before restoring Patch,
adopting the session or arming watchers.

A lost SSH link does not prove provider death. Restart preserves accepted passes
with no recorded stop as waiting for their remote result. Reconciliation handles:

- Unreachable host or still-running provider: wait without a timeout.
- Stopped pass with a complete journal: finalize the original task.
- Stopped incomplete pass, or no accepted prompt: fail visibly for human Retry.
- Already consumed/settled pass: do nothing.

Owners mark transport outages while reopening stages, reading, validating or
persisting results; these keep recorded finalization waiting. A reachable host
reporting a removed, replaced or unowned stage is a real failure. Reading one
part successfully does not make later reads reliable. Once a live stream has
recorded its stop, reconciliation never rereads it; a later settlement outage
is reported rather than parking work whose result was already consumed.

Pause of waiting work requires a reachable host and proven process identity.
There is no delayed kill intent. If the provider already completed, or completes
before Stop reaches it, Pause refuses and preserves its journal for finalization.
Unreadable journals remain unresolved. Active reconciliation participates in
maintenance draining and bounded shutdown.

## Seed and Refresh ingestion

Seed/Refresh alone ingest provider-native history. Context supplies execution-host
log roots, `last_refresh_at`, current graph/research rendering, repository pointers,
selected packages and optional human input. The agent reads live logs in place.
RCP only preflights existence/readability, reporting nonblocking failures; it does
not normalize, cache, transfer or project ordinary live log content, nor maintain
per-log cursors, coverage truth or reports. OpenCode has no supplied log roots
because its sessions live in SQLite.

Personal-to-team transfer uses the native conversation index to select complete
sources by recorded project working paths, best-effort with nonblocking skipped
source diagnostics. It copies original transcript bytes into read-only
`<RCP_DATA_DIR>/project-sources/<project-id>/provider-history/<provider>/`.
Seed/Refresh reads these beside the execution account's live roots. Local runs
read project storage; remote runs stage the validated inventory as immutable
input. Resume requires the same fingerprint; missing/changed staging requires
clean Retry, and missing/corrupt durable imported bytes block ingestion.

RCP chats transfer for human display only, excluded under the
[native chat context rule](conversations-episodes-and-watchers.md#native-chat-context).
Imported sources never enter canonical `.research`, provider homes or rebuildable
caches and grant no session recovery, credential or execution authority.

Only committed Seed/Refresh Patch advances the overlap-tolerant timestamp.
Failure, pause, interruption or rejection leaves it unchanged. Correction is
scratch-only and offline over retained inputs; provider-owned read-only fan-out
leaves the root as the sole Patch writer.

## Provider readiness and native skills

An app-process service coalesces startup executable, version, authentication,
model and machine probes. Navigation does not own warmup or block ordinary use.
Profiles probe what their CLI can enumerate and declare the rest: Codex models
and per-model efforts, Claude declared model aliases and probed provider-wide
efforts, OpenCode models and optional reasoning variants. Unreadable catalogs
use the declared list; that cannot approve an explicitly saved unknown model.

Turns and credential-touching readiness/skill probes share one startup gate per
provider and execution account. They acquire it before environment preparation
and login-generation capture; turns recheck eligibility before spawn. Probes
hold it throughout; turns hold until a provider-authored line and the minimum
stagger, with a bounded maximum hold. Broker readiness is not provider output.
No process is signalled before the minimum hold, including Pause and probe
timeout, so a refresh can finish its credential write.

Local accounts also use an advisory lock under their RCP directory across RCP
processes. Only contention retries; filesystem lock errors fail the launch and
release the in-process lock. OS exit releases the file lock. Remote gates cover
one RCP process only, and different spellings of the SSH destination form separate
gates. Mid-turn provider refresh remains outside this startup protection.

Credential-touching readiness is durable per `(provider, host, executable)` and
version. Implicit reads run `--version` under the gate and reuse matching answers.
Refresh, version change, verify, sign-in and sign-out invalidate them. Native
skill inventory similarly reuses matching executable/version/probe-command
results; explicit Refresh probes all targets, including edited native skills.

Claude's zero-cost Work startup probe uses enforced settings, stream-json and
closed empty stdin. It proves CLI acceptance, not containment. Its availability
and diagnostic are separate from general readiness and shown for profiles that
can launch Work, including chat. Work/orchestrate enforce it with the version
floor; Discuss and Paper do not. Transport failures stay transport failures.

The first decoded error includes bounded, TTY-filtered stderr; a result lacking
text uses its subtype. Service start/end logs carry only identifiers and RCP
wording: provider, capability, runtime, host, executable, pid, operation, exit,
duration and classification. Raw provider output stays off the journal because
it may contain credentials; the task row owns detailed results.

Typed SSH/rsync exit 255 during stage preparation, readiness, transfer or a silent
turn may trigger bounded transport reattempts with increasing waits. Failure to
start a transport or a local wait timeout is not that signal. Provider terminal
events/errors take precedence over SSH exit codes. Unreachable readiness is not
cached. Accepted supervised passes instead use
[remote finalization](#a-remote-turn-outliving-its-connection).

Reattempts use the same native-session recovery as human Retry. Atomic admission
refuses a second continuation if another path already claimed one. Shutdown waits
for admission already in progress, then pauses its child. Refused attempts retain
remaining waits; startup rearms the durable sequence at its current position.
A repeat of the preceding failure ends the ladder. Human Retry retires that
verdict before insertion so a new failure gets its own ladder; revoked login
requires sign-in. Attempts, refusals and exhaustion remain receipts.

Work recovery uses its lineage's durable prompt contract, never the bounded,
prunable `agent_prompt` diagnostic alone. Experiment recovery before prompt
composition reruns the same invocation kind and records
`experiment_uncomposed_rerun`, retaining the newest context candidate. A composed
legacy prompt with no candidate still refuses recovery.

Automatic launches wait for `AUTOMATIC_LAUNCH_AWAKE_SECONDS` of sustained wake.
Watchers, reports and orchestration inputs stay pending; transport waits rearm
without spending attempts. Already-admitted wake redispatch and human launches
are exempt. `machine_sleep` compares platform clocks including/excluding sleep;
unsupported or unreliable pairs impose no hold.

Revoked login is `provider_auth`: stop transport retries and direct sign-in,
without permanently removing recovery controls. A dropped native session starts
clean recovery, including explicit episode rebinding; `session_limit` retains
its session because an account limit may lift. RCP does not infer limit scope
from prose. Humans may change provider/model/reasoning where recovery supports it.

Native skill inventory is app-scoped, separate from official packages. Startup
refreshes after readiness; success atomically replaces inventory and failure
retains it visibly stale. Project open and launch do not refresh. Selection binds
provider, machine, successful version, hash and name per turn. It cannot change
launch flags or scope; provider rejection fails visibly without fallback.

## Official skills and workflows

Project Settings selects official packages. Every official skill is enabled by
default unless the human saves an explicit selection, including an explicit
empty selection. Official workflows remain opt-in.

Only selected packages are staged to the execution machine as immutable,
content-addressed directories. The agent receives compact id, version,
description, dependency, and exact pointer metadata; package bodies are never
embedded into launch prose. Slash completion inserts only the visible token and
a per-turn invocation pointer. Packages supply methods and examples under the
current task contract; they do not replace its authority, output channels, or
filesystem boundary. The same method may serve an ordinary worker, a bounded
Experiment, or the orchestrator without importing another role's permissions.

Graph-writing contracts include shared authoring methods and a local causal
check, using only inputs the task permits. A planned empirical prerequisite
names its precursor Experiment and intended handoff; it does not create Evidence
before an observation exists. For example, plan a calibration before recording
its measured Evidence and the Decision that Evidence informs. A smoke or
validation Experiment is never blocked on the infrastructure it exists to
verify: that setup is written into its own design, the smoke carries
`blocked_by` only for a constraint its run cannot remove, and a downstream main
Experiment keeps the `blocked_by` edge to the Blocker the smoke's Evidence will
address. Decision options are written only after every distinct choice has been
investigated with equal care, at one level of detail, with any leaning recorded
in `rationale` rather than in option order, length, or wording. Optional
`graph-audit`, `experiment-causality`, and `evidence-triage` packages add deeper
methods and examples. Programmatic quality advice belongs to the existing live
Patch validator, not a separate mandatory scanner package or model call.
Nonblocking flags return in a valid result's `messages` with exit code zero;
blocking errors take priority while the Patch is invalid. Quality advice does
not require another provider turn and cannot change acceptance or graph authority.

## Network behavior

Every user-facing Seed/Refresh, Discuss, Work, Experiment, Auto-research, merge,
and Paper surface retains its provider-native public-web behavior as defined by
its contract. Exact project write roots do not add a new network restriction.
Generic scratch-only Patch correction remains offline where its retained
contract requires that.

## Host browser runtime

On Linux, the browser daemon and Chromium run under the launch's hidden-path
policy inside the systemd job command. The host persists the policy fingerprint
and changes a live daemon's policy under the host lock, preserving its profile
and active leases. Hidden-file uploads fail; other uploads, downloads, and
browser capabilities remain available. On macOS the daemon stays unwrapped
because Chromium's sandbox cannot nest inside Seatbelt; browser file operations
remain unhidden; Settings and doctor show that exception. An enforcement failure
keeps the browser available and contributes to the effective scope status.

`browser/` owns optional headless browser installation and sessions. It is
separate from provider launches. The launch integration supplies a stable owner
token and the stage workspace. The runtime exports ensure, release, and close;
it never installs tools during ensure. When a broken CLI cannot probe retained
sessions during Install, only a proven-stopped OS owner permits repair; live or
unknown owners block it.

RCP pins Playwright CLI 0.1.22. Each owner has an explicit Chromium executable,
headless configuration, persistent profile, and output directory. Ambient
attachment, headed, profile, and storage-state settings are overridden. Launch
environment overrides are removed. The pinned daemon entry point runs directly
under launchd or a lingering systemd user manager, so the OS owns the daemon
rather than its short-lived CLI launcher. On Linux the daemon, RCP's probes, and
the agent's CLI launcher share `TMPDIR=$XDG_RUNTIME_DIR`, because a service with
`PrivateTmp` (the team server) and the user manager see different `/tmp`
directories and the session socket lives under `TMPDIR`. A missing process owner makes the
browser unavailable; a systemd account without linger reports `linger_disabled`
before an install is offered.

Ensure probes the CLI registry from the stage workspace before starting a
session. It creates no `.playwright` marker and never reopens a live session.
Release checks liveness and ends the active lease. File locks serialize host
operations. The host cap counts managed live sessions; the least recently used
owner without active leases is closed first. A full busy host reports capacity.
Controller restart adopts live sessions and the leases of remote turns still
awaiting settlement. Ensure and close carry those retained lease ids to the host,
which moves them to the current controller epoch and prunes its other old leases.
Normal and recovered turns release through the same durable owner, host, stage,
and lease routing. Recorded-result settlement, Pause, and a proven-stopped remote
provider pass finish retained leases before deferred cleanup; cleanup retries
probe unresolved passes of inactive tasks and stay pending until they stop.

Close uses the CLI's graceful close before removing an explicitly requested
profile together with that owner's page snapshots and logs. A confirmed profile deletion retires the runtime record. A removed
workspace is retired, with its profile, only after its OS owner is proven
stopped, because a new stage gets a new owner; a live or unknown owner remains
fenced without blocking other owners' acquisition. A partly written install
reads as not installed, so Install can repair it. While Install holds the host
lock, readiness and a second Install answer `installing` at once instead of
waiting for the lock; the machine card re-checks until it settles.
Closing an active owner waits for its last lease to finish. Idle timeout
is passed in milliseconds from the seconds-based limit. Failed remote cleanup
and release remain durable and retry on later ensure or install calls, one
queued operation per call. Replayed close requests do not prune old epochs from
stale retention snapshots; a fresh ensure or close owns that pruning. Before
acquisition contacts the host, the controller
journals a release for the exact new lease; handing back a validated grant clears
that recovery request. A lost or invalid reply leaves it for retry, and releasing
an unknown lease is a no-op. Readiness does not run queued cleanup.

## Bulletin commands and context

The target-local Bulletin is untrusted operational hearsay, never graph or
launch authority. Work-capable runs (Work, Experiment, child Work, and the
Auto-research orchestrator and worker) hold the `bulletin` verb while the owning
episode's Bulletin is on; only Auto-research start can turn it off, and a run
with no episode always holds it. Holding it means `bulletin --read [--cursor C]`,
`bulletin --post --key K [--reply-to P] <body-file>`,
`bulletin --edit P --key K <body-file>`, and `bulletin --delete P --key K`;
references come from inline `#node-id` text. That one resolved verb set drives
both the contract's usage lines and command admission. Discuss sees the Bulletin
launch snapshot without holding the `bulletin` verb.

Ordinary Discuss and Work chats, Experiment conversations, child Work chats,
and Auto-research orchestrator chains hold `name "short task name"` only when
storage's resolved launch state permits their first self-name, independently of
the Bulletin toggle. Seated Auto-research workers have no board conversation;
they and Graph consolidation never hold `name`. The same resolved state supplies
the grant and staged naming snapshot, and the granted verbs render the prompt
usage. Eligible agents are asked to choose a short 3–6 word task name within
`AGENT_NAME_MAX_CHARS`. The task binding fixes the caller's conversation:
commands cannot name another agent. Storage refuses a second self-name or any
self-name after a human rename, collapses whitespace, and rejects empty or
over-limit names with structured refusal codes. The name is operational
metadata and grants no graph authority.

Editing and deleting require the stored roster actor identity, allowing later
turns of the same actor to manage their own posts. Receipts make all three
mutations replay-safe within each turn; fresh mutations share the existing
transactional Stop fence.

Launch snapshots and `bulletin --read` include a mentions section first, newest
first within the render budget, followed by member ids and names, agents/roster,
and posts. Budget is allocated to mentions, posts, members, then agents. Agents
write `<@member:USER_ID>` or `<@agent:ACTOR_ID>` tokens using these identities.
Context is captured or explicitly read, never pushed into a running turn.
Mentioning an agent grants no capability and launches nothing.
