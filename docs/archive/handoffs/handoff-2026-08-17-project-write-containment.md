# Provider-native project write containment implementation handoff

Date: 2026-08-17
Status: confirmed and ready to implement

## Purpose

Add a low-cost guardrail so a Work-like invocation launched for project A cannot accidentally write project B's repositories or RCP-owned project data.

This is cooperative-user containment, not hostile-user isolation. Every person admitted to a team is assumed benevolent, task initiation already requires server access and RCP authority, and prompts are not treated as adversarial inputs. Do not add an external process sandbox, container boundary, per-project operating-system account, network restriction, invocation supervisor, or cross-project read barrier.

Use the provider CLIs' existing native workspace/write-root mechanisms. The change is to make the roots RCP computes actually constrain writes instead of launching Work and Orchestrate in provider bypass modes.

## Confirmed trust and boundary model

- Team members and project inputs are trusted.
- Cross-project confidentiality is not a goal. A run may retain the read access already implied by the execution account and supplied context.
- The guardrail covers accidental writes to project data.
- Provider-owned authentication, session, and cache files may remain provider runtime exceptions. They are not general project write roots and must not be widened into access to another project.
- Canonical `.research` state remains writable only by RCP through the Patch/transition path. An agent writes `patch.json` in its task stage; it never writes canonical graph history directly.
- Public-network behavior remains unchanged.

## Current gap to close

RCP already computes `read_dirs` and `write_dirs`, but the Work-like provider commands do not currently enforce the supplied write roots:

- Codex discards the directory arguments and launches `work_auto` and `orchestrate` with its dangerous bypass flag.
- Claude launches those capabilities with `bypassPermissions`; `--add-dir` supplies access but is not a negative write boundary.

Do not preserve those bypass paths and claim the project boundary is enforced elsewhere.

## Canonical write-scope contract

Introduce one resolved launch contract, named consistently in code, that binds an invocation to:

- `project_id`;
- execution host/account identity already used by RCP;
- capability;
- exact task stage/workspace root;
- exact writable repository roots admitted for this task on that execution host;
- any narrow project-owned artifact/cache directory the task contract genuinely writes; and
- a stable fingerprint of the canonicalized roots.

The contract is produced by RCP from the project manifest, repository pointers, task/episode lineage, and the already-selected run scope. The browser, agent prompt, request body, or provider must not be able to add an arbitrary path.

### Writable roots by surface

- Ordinary Work and Experiment-loop turns receive their existing run-scope repository roots, not every repository visible to the service account.
- The Auto-research orchestrator receives the current project's repositories allowed by its project-wide run contract on the chosen execution machine.
- Auto-research child Work and child Experiment turns receive the repository roots selected for that child within the same project.
- Every launch receives only its exact task stage/workspace as scratch.
- Discuss, Paper, Seed/Refresh, and other narrower capabilities retain their current narrower write behavior.

No scope may include:

- another project's repository root;
- a parent directory merely because it contains several projects;
- the RCP application data directory or SQLite database;
- canonical `.research` as an agent-writable root;
- the execution account's home directory as a general root; or
- a broad temporary directory.

Canonicalize roots on the execution machine before launch. Reject a missing root, a root that no longer matches the project's registered repository, a project/host mismatch, or a resumed stage bound to a different project. Deduplicate exact roots without broadening them to a common parent.

## Provider launch behavior

### Codex

For Work and Orchestrate:

- remove `--dangerously-bypass-approvals-and-sandbox`;
- use Codex's native unattended `workspace-write` mode;
- make the exact task workspace the working directory;
- add only the admitted additional repository roots through the provider's native additional-directory mechanism;
- keep approval policy non-interactive and network access unchanged; and
- apply the same scope on a resumed native session.

The command builder must consume, not discard, the resolved write roots.

### Claude

For Work and Orchestrate:

- remove `bypassPermissions`;
- use the installed Claude CLI's native unattended filesystem write allow-list/sandbox settings;
- allow writes only to the task workspace and admitted repository roots;
- keep public WebSearch/WebFetch behavior unchanged; and
- apply the same scope on resume.

Probe the pinned/supported Claude CLI version and use its current native setting shape. Do not emulate the boundary in prompts. If a supported provider version cannot enforce the declared write roots without interactive approval, fail that Work-like launch with a specific provider-compatibility error rather than silently restoring broad bypass access.

### Local and SSH execution

The same resolved contract applies locally and remotely. Remote command construction must carry the exact remote roots and must not substitute local paths or a broad remote working directory.

Provider authentication continues to follow the execution account, as designed. This handoff does not change team account ownership or SSH trust.

## Resume, retry, and correction

A continued native session is not permission to reuse an old filesystem scope blindly.

Record the write-scope fingerprint in the launch receipt and durable task binding. Before resume, retry, watcher wake, or Patch correction:

- recompute the current project scope;
- verify project id, execution host, task stage, and root fingerprint against the saved binding; and
- refuse the continuation if the scope belongs to a different project or has changed incompatibly.

A legitimate repository relocation or run-scope change should require a fresh provider session/task rather than silently widening the existing one.

Every Work-like continuation path must use the same scope resolver. Do not fix only the first root turn while leaving corrections, Auto-research workers, Experiment-loop wakes, or remote resumes on bypass behavior.

## Audit and errors

The launch receipt must record, without copying file contents:

- project id;
- execution host;
- capability;
- canonical writable roots;
- scope fingerprint;
- provider enforcement mode; and
- whether the launch was fresh or resumed.

Errors must say which declared project root is unavailable or unsupported. They must not expose credentials or unrelated filesystem contents.

## Non-goals

Do not add:

- Bubblewrap, Landlock, Docker, VM isolation, chroot, or a second service account;
- malicious-prompt or malicious-member defenses;
- file-read secrecy between projects;
- subprocess or wall-clock policing;
- new network policy;
- repository version-control branches; or
- a user-configurable permission toggle.

## Important seams

Expected implementation seams include:

- `src/rcp/providers.py`;
- `src/rcp/agents/launcher.py`;
- `src/rcp/runs/chat.py` scope construction;
- `src/rcp/runs/shared.py` launch receipts and common continuation path;
- `src/rcp/runs/work.py`;
- `src/rcp/runs/auto_research_stream.py` and child launch paths;
- Experiment-loop and watcher-wake launch paths;
- remote command/stage handling under `src/rcp/transport/`; and
- provider and run tests.

Keep one scope resolver and one provider-neutral contract. Do not reproduce project-root validation separately in each run module.

## Acceptance and verification

Do not create a new scenario merely because this is a bug-prevention change. Extend the active boundary/remote-run acceptance contract that remains after the documentation cleanup, preferably the successor of S74 and S102, with this durable promise:

> A Work-like task launched for one project can write only its own task stage and the repository roots RCP admitted for that project and task. RCP does not rely on prompt wording, and it refuses a provider launch when the provider cannot enforce the declared roots.

Required tests:

1. Project A command construction never includes project B roots even when both are owned by the same execution account.
2. Codex Work, Orchestrate, resume, and correction paths use native workspace-write and never use the dangerous bypass flag.
3. Claude Work, Orchestrate, resume, and correction paths use the supported native write allow-list and never use `bypassPermissions`.
4. Local and remote paths are canonicalized against the correct project and host.
5. A stale or cross-project stage/session binding fails before provider launch.
6. Canonical `.research`, the application database, and broad parent directories are not agent-writable roots.
7. Auto-research root, child Work, child Experiment, and watcher/correction continuations all use the common resolver.
8. Existing public-network access and read-only capabilities remain unchanged.
9. Launch receipts accurately report the enforced roots and mode.
10. Live provider probes confirm an attempted write outside the admitted project roots is denied while an admitted repository write succeeds.

## Completion

Update the agent/provider specification to describe cooperative project write containment accurately. Do not describe it as a hostile security boundary. Archive this handoff after implementation and verification.
