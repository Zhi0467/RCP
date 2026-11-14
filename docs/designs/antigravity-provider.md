# Antigravity CLI (`agy`) as an RCP provider

Status: design, not built. Probed against `agy` 1.5.1 on macOS (arm64) on
2026-11-13. Every prompt-running probe ran in a throwaway folder under `/tmp`,
with no dangerous-permissions flag and none of the user's own grants.

This document extends the three-provider registry
(`src/rcp/providers/__init__.py`) to a fourth profile, `agy`. Its rules move into
[providers-and-containment](../specs/providers-and-containment.md) before merge.
Then this file is deleted.

## Summary

`agy` fits RCP's profile shape well. It has a JSONL stream, multi-turn stdin,
per-launch settings, deny-beats-allow file rules, a model catalog, a skill
inventory, and a zero-token authenticated quota call. Its containment story is
close to Claude's: the file tools are bounded and the Work shell is not.

Three facts make it different from every existing provider. Each one needs a
decision (see [Open decisions](#open-decisions)):

1. **An unapproved action ends the turn.** In headless mode, a tool call that
   no rule allows is "soft-denied". The run stops at once with an empty reply
   and a `denied_actions` list. An explicit `deny` rule behaves differently: the
   tool fails and the turn goes on.
2. **Hooks cannot be turned off.** `hooks.json` files in the global config, in
   plugins, in the working directory, *and in every `--add-dir` repository* run
   their commands outside all permission rules. No flag or setting disables
   them.
3. **The user's global settings leak in.** A persisted `agentMode: "auto"`
   silently approves commands in headless runs. RCP must override it on every
   launch. `--settings` *replaces* the permission lists, so the user's own allow
   rules cannot widen RCP's.

There is no native fork, no shell-timeout control, and no way to route shell
commands through RCP's hidden-read wrapper.

## Probe evidence

Each line is something observed with 1.5.1, not read from docs. Paths are
placeholders.

| # | Probe | Observed |
| --- | --- | --- |
| P1 | `agy -p "…" --output-format stream-json --mode plan --sandbox --model gemini-3.8-flash-low` | Exit 0. Lines: `{"event":"init",…}`, `step_update`s, then one `{"event":"result",…}`. `conversation_id` appears on every line. |
| P2 | Same, with `--conversation <id>` | Resumed the same id. The model saw the prior turn. |
| P2b | `--mode plan` | Sends the prompt as `/plan <prompt>`. The model wrote a plan file into `~/.gemini/antigravity-cli/brain/<id>/` (outside the workspace, no approval needed). Plan mode is a planning workflow, not a read-only mode. |
| P3 | `--settings '{"permissions":{"deny":["read_file(<ws>/secret.txt)"]}}'` | `view_file` failed with "Matches user-configured deny rule". The turn continued. |
| P4 | `--sandbox`, no allow rule, run `wc` | Ran unprompted. Sandboxed commands run without approval. |
| P5 | No sandbox, allow only `command(wc)`, run `cat` | **Ran.** The cause was the user's global `agentMode: "auto"`. |
| P6 | `--settings '{"agentMode":"default"}'` | Exit 1: `unrecognized value "default"`. The default mode is spelled `""`. |
| P6b | `agentMode:""`, allow `command(wc)`, run `wc` then `cat` | `wc` ran. `cat` was soft-denied: "permission check failed for unsandboxed …". **The run ended there**, with `response: ""`, `denied_actions:[{"action":"command"}]`, exit 0, and a `jetski: no output produced …` line on stderr. |
| P7 | Same, run `head` (allowed in the user's global list) | Soft-denied. `--settings` `permissions.allow` replaced the global list. |
| P8 | `--mode accept-edits`, `allowNonWorkspaceAccess:false`, allow `write_file(<extra>)`, deny `write_file(<ws>/protected)`; four `write_file` calls | Workspace file written. Protected: tool error, turn continued. Extra root: written. Outside: soft-denied, turn ended. |
| P9 | `.agents/agents/rcp-probe.md` in cwd with `tools: [view_file, list_dir, grep_search]`, `--agent rcp-probe` | The agent loaded from an untrusted `/tmp` folder. The model reported only two tools. Input tokens fell from ~11k to ~5.6k. `init.tools` still lists the full static set, so it is not evidence. |
| P10 | `PreInvocation` hooks in `<cwd>/.agents/hooks.json` and `<repo>/.agents/hooks.json`, `--add-dir <repo>` | **Both ran** (marker files created). |
| P11 | `SHELL=<logging wrapper>` | Never invoked. `run_command` does not use `$SHELL`. |
| P12 | `grep_search`, `list_dir` by name | "not available in the current environment" despite `init.tools`. The model reaches for shell `grep` instead, which a Discuss policy soft-denies. |
| P13 | `agy -p /usage --output-format json` | Zero tokens, `conversation_id:""`, per-bucket `remaining_fraction`. Signed out: exit 1, `"error":"authentication failed or timed out"`, and an OAuth URL on stderr. |
| P14 | `agy -p /skills --output-format json` | `command.data.skills[]` with `name`, `description`, `path`, `builtin`, `model_invocable`. |
| P15 | `agy models` | Tab-separated `slug<TAB>label`. The effort is part of the slug (`gemini-3.8-flash-low/-medium/-high`). Signed out: exit 1, "Please sign in". |
| P16 | `--model bogus` | Exit 1. A `result` line with `status:"ERROR"` and the error text on stderr, listing the valid models. |
| P17 | `--input-format stream-json`, two `{"event":"user",…}` lines 2 s apart, `queuedMessages:"queue"` | Two turns, one `result` each, in FIFO order, in one process and conversation. Inputs carry no ids, and results name none. |
| P18 | Usage | Each `agent_response` step with `state:"DONE"` carries `usage` for that model call. `input_tokens` excludes `cache_read_tokens`. `result.usage` is cumulative over the **whole conversation**: on resume it included the previous process's turn. |
| P19 | `HOME=<empty dir>` | Signed out. The login is found through `HOME` (`~/.gemini/antigravity-cli/`). Side effect: `agy` started an OAuth sign-in in the browser. Never repeat this probe on a human's desktop. |

Docs used: [headless mode](https://antigravity.google/docs/cli/headless.md),
[permissions](https://antigravity.google/docs/permissions.md),
[terminal sandbox](https://antigravity.google/docs/sandbox.md),
[settings](https://antigravity.google/docs/settings.md),
[hooks](https://antigravity.google/docs/hooks.md),
[custom agents](https://antigravity.google/docs/subagents.md), and the bundled
`~/.gemini/antigravity-cli/cache/CHANGELOG.md`.

## Contract checklist

Each row is one thing a profile must do, where RCP owns it today, and how
`agy` does it. **Native** means agy does it as RCP needs. **Adapter** means the
profile translates. **Missing** names the consequence.

### Identity, install, version

| Contract | Owner | Existing providers | agy | Status |
| --- | --- | --- | --- | --- |
| `id`, `label` | `base.py:398` | `codex`, `claude`, `opencode` | `agy`, "Antigravity" | Native |
| Executable location | `install_paths`, `base.py:418`; `launcher.py` | `.opencode/bin/opencode`, … | `~/.local/bin/agy` (a single Go binary of 200 MB) | Native |
| Native update | `native_update`, `base.py:367` | `self_update_args` | `agy update` | Native |
| Version | readiness `--version` | all | `agy --version` prints `1.5.1` | Native |
| Version floor | `work_like_minimum_version`, `validate_readiness_version` `base.py:478` | Claude 2.1.233; OpenCode 1.18.30 on every capability | 1.5.1 on **every** capability: `--settings` replacing lists, `agentMode:""`, and deny semantics were probed only there | Native |
| Dependency registry | `src/rcp/dependencies.py` (AGENTS.md) | each CLI declared | Declare `agy`: purpose, roles, platforms (macOS, Linux), tier optional | Adapter |

### Launch and wire protocol

| Contract | Owner | Existing | agy | Status |
| --- | --- | --- | --- | --- |
| Runtime ids and choices | `legacy_runtime_id`, `default_runtime`, `runtime_aliases`, `runtime_choices` | Claude `stream-json`, OpenCode `run-json`, Codex `app-server`/`exec` | One runtime, `agy.stream-json.v1`, public name `stream-json` | Native |
| argv, prompt on stdin | `ProviderProfile.command` `base.py:588`; `ProviderTurn.initial_input` | Claude writes a stream-json user line | `agy --input-format stream-json --output-format stream-json` plus flags. The prompt is `{"event":"user","message":{"content":…}}`. stdin stays open | Native |
| Per-launch policy | `ProviderTurn.command` / `environment` | Claude `--settings`; OpenCode `OPENCODE_CONFIG_CONTENT` | `--settings '<json>'`: session-only, never saved, rejects bad values with exit 1 | Native |
| Stream decode | `decode_event` | profile-owned | `step_update` with `step_type: agent_response` is message text (`text_delta`). `result.response` is the **answer**. `result.status != SUCCESS` or a `result.error` is an **error**. The first line's `conversation_id` is the **session** | Adapter |
| Turn end (local) | `ProviderTurn.receive_line` | Claude `result`; OpenCode wrapper line | One `{"event":"result"}` per user line sent. Complete when results seen = inputs sent, or at the first non-`SUCCESS` result | Adapter |
| Turn end (remote fence) | `remote.py` `TurnFence`, `turn_fence.py:19` | per provider | `AgyStreamTurnFence`: count `{"event":"user"}` inputs and `{"event":"result"}` outputs. Ship it in `providers/agy/remote.py`, stdlib only | Adapter |
| Errors on stderr | `probe_failure_evidence`, `credential_failure` | per provider | An `AGY_ERROR: {json}` line with canonical status, HTTP/gRPC code and retryability, exit 3. `error:` lines, exit 1 for usage errors | Adapter |
| Partial cut-off | — | — | `result.finished_reason` is `print_timeout`, `interrupted` or `error` | Adapter: map to error text |
| Launch degradation | `launch_degradation` `base.py:617` | Claude effort warning | Bad `--model` or `--effort` fails outright (P16), so there is nothing to add | Native |
| Oversized events | launcher per-event ceiling | stdin-open providers fail the pass | Same as Claude: stdin stays open, so an oversized event fails the pass | Native |

### Sessions

| Contract | Owner | Existing | agy | Status |
| --- | --- | --- | --- | --- |
| Resume | `command(session_id=…)` | `--resume`, `--session`, `thread/resume` | `--conversation <id>`. The id is kept (P2) | Native |
| Fork (branch a chat) | `fork_session_id`; `SessionFormat.prepare_session_fork` | Claude `--fork-session` after a transcript copy; OpenCode `--fork`; Codex `thread/fork` | No flag. Conversations are per-id SQLite files in `~/.gemini/antigravity-cli/conversations/<id>.db` (tables `steps`, `trajectory_meta`, …), and copying one is unsupported | **Missing**: [decision D4](#d4-branching-a-chat-from-an-agy-session) |
| Session indexing | `session_format`, `*_session_roots_field`, `config.py:224` | Claude and Codex JSONL; OpenCode none | SQLite, not JSONL | **Missing** by choice: no session format, as with OpenCode. RCP does not index agy history |
| Stale session | `classify_terminal_error` `__init__.py:136` | Codex "no thread with id" | Not observed. Leave as `provider_error` until a real missing-conversation message is captured | Missing, safe default |
| Native memory off | spec "Native provider memory is off" | `--disable memories`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | agy has "knowledge" (`delete_knowledge` tool) and a per-conversation `brain/` folder. No exposed switch was found. A staged agent's `tools` list can leave out the knowledge tools | Adapter (tools list), unverified that knowledge is never injected |

### Capability and containment

| Contract | Owner | Existing | agy | Status |
| --- | --- | --- | --- | --- |
| Capability modes | `command` per `AgentCapability` `base.py:29` | Claude modes per capability | `--mode accept-edits` for writers. `agentMode:""` (default) for Discuss and readers. Never `auto` (a model reviewer approves commands) or `--dangerously-skip-permissions` | Adapter |
| Override user mode | — | Claude `--setting-sources`; OpenCode agent precedence | `--settings` sets `agentMode` (P5, P6b). The `--mode` flag also works for `accept-edits` and `plan` | Adapter, **required** |
| Tool set per capability | Claude `_CLAUDE_TOOLS` | Claude `--tools`; OpenCode deny-first rules | A staged agent `.agents/agents/rcp-<digest>.md` in the stage cwd with `tools: […]`, selected by `--agent rcp-<digest>` (P9). Named by a digest of its content, so no user agent can collide | Adapter |
| Work write roots | `ProviderWriteScope`; `_require_project_write_scope` `base.py:702` | Claude `Edit(…)` allows; OpenCode edit rules | Workspace = cwd + `--add-dir` roots (edits auto-approved in `accept-edits`). `write_file(<root>)` allows for other writable roots. `write_file(<protected>)` denies. `allowNonWorkspaceAccess:false` | Native (P8) |
| Protected path inside a root | spec "Machine writable paths" | Claude: deny beats allow, so a protected parent of the own stage is left undenied | Same: deny beats ask beats allow | Native, same exception as Claude |
| Work shell | `canonical_state_boundary` `base.py:613` | Claude and OpenCode `file_tools_only` | `command(*)` allow, no `--sandbox`. The agy sandbox cuts networking on Linux (namespaces) and filters sockets on macOS, so compute would break, as with Claude's sandbox | Adapter; boundary `file_tools_only` |
| Discuss read context | spec "Claude" section | Claude per-dir `Read/Glob/Grep` allows, never `--add-dir` | `read_file(<dir>)` allows, never `--add-dir`: `--add-dir` makes a folder an auto-editable workspace in `accept-edits`, and it also loads that folder's hooks (P10) | Adapter |
| Discuss shell | broker client and browser grants | Claude `Bash(<client>:*)`; OpenCode deny-first | `command(<exact client argv>)`, `command(playwright-cli)`, `command(<preview>)` allows. Every other command is soft-denied, **which ends the turn** | Adapter + [decision D1](#d1-a-denied-action-ends-the-turn) |
| Paper coach | `_paper_write_roots` `base.py:688` | changes folder + attached artifacts | The workspace (cwd) gets no write grant, as for the other providers. `write_file(<root>)` allows the changes folder and each attached artifact folder. `command(*)` is allowed, as for Work | Adapter |
| Session summary | `session_summary` capability | Codex read-only; Claude no tools; OpenCode refuses | Staged agent with an empty tool list, plus `agentMode:""`. Unproven: the docs give `tools` a default of `[]`, so empty may mean "default set". Refuse until probed | Missing until probed |
| Hooks | — | Claude `disableAllHooks`; OpenCode `--pure` | **No switch.** Global `~/.gemini/config/hooks.json`, plugin hooks, `<cwd>/.agents/hooks.json` and every `--add-dir` root's `.agents/hooks.json` run (P10) | **Missing**: [decision D2](#d2-hooks-cannot-be-disabled) |
| MCP servers | Claude strict empty MCP; OpenCode deny-first | — | `mcp(*)` defaults to ask, so a call is soft-denied. The staged tools list can leave out `call_mcp_tool` | Adapter |
| Plugins, skills, rules | skill probe flags | per provider | Load from `~/.gemini/config/` and every `.agents/` between cwd and the project root. No `--pure` equivalent | Accepted. Same as Claude's user setting source |
| Native browser | browser grant | Playwright CLI only, no MCP | agy has its own `browser_*` tools and `execute_url` rules. Leave them out of the staged tool list, so RCP's Playwright grant stays the one browser path | Adapter |
| Scheduling and messaging tools | Claude `--tools` reasoning | — | `schedule`, `send_message`, `manage_inbox`, `run_workflow`, `search_marketplace`, `generate_image`, `define_subagent` exist. Leave them out of the staged tool list | Adapter |
| Provider runtime storage | spec "Provider auth/session/cache exceptions" | `~/.claude`, `~/.codex` | Always writable without approval: `~/.gemini/antigravity-cli/brain/<conversation>/` (P2b) | Native exception |

### Hidden reads and credentials

| Contract | Owner | Existing | agy | Status |
| --- | --- | --- | --- | --- |
| Native read denies | `HiddenReadScope` rendering | Claude `Read(…)` denies; OpenCode `read` denies | `read_file(<path>)` denies (P3). "Deny read implies deny write" | Native for file tools |
| Shell wrapper | `CLAUDE_CODE_SHELL_PREFIX`; OpenCode `SHELL` | wrapper per launch | `$SHELL` is ignored (P11). No prefix lever was found | **Missing**: every agy launch reports `unhidden` with `provider_native_tools_uncovered` |
| Login file hidden from other providers | `staged_hidden_read.py:86`, `space_machines.py:273` (hard-coded per id) | three files | `~/.gemini/antigravity-cli/antigravity-oauth-token`, `~/.gemini/oauth_creds.json` | Adapter: move the list into a profile attribute |
| Credential failure | `credential_failure` `base.py:635` | Claude 401 | `AGY_ERROR` JSON carries a canonical status. Only a really revoked login should be matched, so return False until one is observed | Missing, safe default |

### Login and readiness

| Contract | Owner | Existing | agy | Status |
| --- | --- | --- | --- | --- |
| Presence check | `auth_command`, `is_authenticated` | `claude auth status`, `opencode providers list` | No `auth` subcommand. `agy models` exits 1 when signed out (P15) and lists models when signed in | Adapter |
| Liveness check | `login_probe_command` `base.py:495` | Claude one haiku turn | `agy -p /usage --output-format json`: an authenticated server call that spends zero model tokens (P13). It is the best liveness check of the four providers | Native |
| Interactive login | `login_command` | `claude auth login` | `agy` with no arguments (TUI sign-in), or `agy acp --login` for a terminal-only host | Native |
| RCP-managed sign-in | `auth.py`, `provider_auth` | Claude and Codex | Not in v1: [decision D3](#d3-sign-in-and-its-persistence) | Missing by choice |
| Work startup probe | `work_like_probe_command` | Claude | A `--settings` render plus `--mode accept-edits` with stdin closed and no prompt. Untested: it may exit without validating settings. Leave it None until probed | Missing until probed |

### Models, usage, status, steering, skills

| Contract | Owner | Existing | agy | Status |
| --- | --- | --- | --- | --- |
| Catalog | `catalog_command`, `parse_catalog` | Codex and OpenCode probed; Claude declared | `agy models`: one `ModelChoice` per slug, empty `reasoning`, so no separate effort picker. The effort lives in the slug (P15) | Native |
| Effort | `--effort` | per model | `--effort` exists, but the slugs already fix the level, and passing both is untested. Never pass `--effort` | Native via slug |
| Usage | `decode_usage`, `usage_profile` | per provider | `agy.step.v1`: one `ProviderUsage` per `agent_response` step with `state:"DONE"`. processed = `input_tokens + cache_read_tokens`, generated = `output_tokens`. **Never `result.usage`**: it is cumulative over the conversation (P18) | Adapter |
| Context tokens | `AgentStatusDelta.context_tokens` | Claude per message | `input_tokens + cache_read_tokens` of the latest main-agent step | Adapter |
| Context window | `context_window` | Claude result | Not reported | Missing: the panel shows no window |
| Compaction | `compacting`, `compaction` | Claude, Codex app-server | A `step_type:"checkpoint"` is documented but its fields were not observed | Missing until observed |
| Subagents | `AgentSubagent` | Claude, Codex | `step_update.subagent_info[]`: `type_name`, `role`, `conversation_id` | Adapter, unprobed |
| Steering | `ProviderRuntime.steering_behavior` `base.py:275` | Claude `queue`, Codex `inject` | `queue`. A second user line runs as the next turn (P17). With no input ids, RCP attributes results by FIFO count. Always set `queuedMessages:"queue"` in `--settings`, since the user may have `send-immediately`, which interrupts the turn | Adapter |
| Skills inventory | `skill_probe`, `parse_skills` | per provider | `agy -p /skills --output-format json` → `command.data.skills[]` (P14), `protocol: "jsonl"` with one line | Native |
| Skill token | `native_skill_token` | `/name` | `/name`: print mode expands slash skills | Native |
| Autocompact | `autocompact_hint`, `canonical_autocompact` | Claude only | `AGY_CLI_AUTO_COMPACT_OVERRIDE` exists in the binary, but its meaning is undocumented | Missing: `autocompact_hint = None` |
| Shell timeout | `shell_timeout_default_seconds`, `canonical_shell_timeout` `base.py:435` | Claude and OpenCode env vars | No lever. Long commands become background tasks (`WaitMsBeforeAsync`, `command_status`), and headless runs wait for them | **Missing**: needs a profile flag, see [Generic changes](#generic-changes-outside-the-package) |

## The profile

One package, `src/rcp/providers/agy/`, holds everything RCP knows about `agy`:

- `profile.py`: `AgyProfile(ProviderProfile)` with the attributes above, the
  argv, the `--settings` policy, the staged agent file, the decoder, usage, and
  status.
- `remote.py`: `AgyStreamTurnFence`, stdlib only, shipped by `remote_bundle`.
  No session format.
- No `auth.py` in v1, so the Sign-ins fold shows the CLI's own login command.

Registration is the one line in `PROVIDERS`. Every imperative piece lives
inside the profile:

```text
AgyProfile
  id = "agy"; label = "Antigravity"
  install_paths = (".local/bin/agy",)
  native_update = ProviderNativeUpdate(self_update_args=("update",))
  legacy_runtime_id = "agy.stream-json.v1"; default_runtime = "stream-json"
  turn_fences = {"agy.stream-json.v1": AgyStreamTurnFence}
  usage_profile = "agy.step.v1"
  work_like_minimum_version = (1, 5, 1)       # floor for every capability
  session_format = None
  autocompact_hint = None
  shell_timeout_supported = False             # new base attribute
  native_tools_hide_reads = False             # new base attribute (see below)
  login_files = (".gemini/antigravity-cli/antigravity-oauth-token",
                 ".gemini/oauth_creds.json")  # new base attribute
  supports_session_summary = False            # new base attribute
  auth_command          -> [agy, "models"]
  login_probe_command   -> [agy, "-p", "/usage", "--output-format", "json"]
  login_command         -> [agy]
  catalog_command       -> [agy, "models"]
  skill_probe           -> [agy, "-p", "/skills", "--output-format", "json"]
  project_write_enforcement_mode -> "agy.permission-rules.v1"
  canonical_state_boundary       -> "file_tools_only"
  runtime(...)          -> _AgyStreamRuntime (steering_behavior = "queue")
```

### Launch command

Every launch, any capability:

```text
agy --input-format stream-json --output-format stream-json
    --model <slug>                       # when chosen
    --agent rcp-<digest>                 # staged in <cwd>/.agents/agents/
    --settings <policy json>
    [--mode accept-edits]                # writers only
    [--conversation <id>]                # resume
    [--add-dir <root> ...]               # Work-like only: admitted repo + memory roots
```

Never `-p`. Print mode cannot take stdin turns, and `-p` with stream input is an
error. Never `--sandbox`, `--mode auto`, `--mode plan`, `--remote-agent`,
`--remote-control`, or `--dangerously-skip-permissions`. A unit test asserts
these flags never appear in any rendered argv.

### Policy JSON

```json
{
  "agentMode": "",
  "toolPermission": "request-review",
  "allowNonWorkspaceAccess": false,
  "enableTerminalSandbox": false,
  "queuedMessages": "queue",
  "artifactReviewPolicy": "always-proceed",
  "permissions": {
    "allow": ["<per capability>"],
    "ask": [],
    "deny": ["<protected write paths>", "<hidden read paths>"]
  }
}
```

`--settings` replaces each permission list it names (P7), so all three are
always written, even when empty. The user's global allow rules then cannot widen
a launch, and their deny rules cannot narrow one. That is stricter than Claude,
whose user denies still narrow.

`artifactReviewPolicy: always-proceed` keeps an implementation-plan review from
stalling a headless turn. The changelog says headless already proceeds through
plan review. Setting it explicitly removes the dependency on the user's value.

Allow rules per capability:

| Capability | allow | Tools in the staged agent |
| --- | --- | --- |
| Work, orchestrate | `command(*)`, `read_file(*)`, `write_file(<root>)` per writable root outside the workspace, `read_url(*)` | file read/write/edit, `run_command`, `command_status`, `send_command_input`, `search_web`, `read_url_content`, subagent tools |
| Discuss | `read_file(<dir>)` per read dir, `command(<client argv>)`, browser commands when granted, `read_url(*)` | file read/write/edit (write_file covers only the stage and memory, through the workspace and allows), `run_command` only when a client or browser grant exists, `search_web`, `read_url_content` |
| Seed, Refresh, scratch Patch | as Discuss, no browser | as Discuss |
| Paper coach | `command(*)`, `read_file(*)`, `write_file(<root>)` per coach root | as Work |
| Session summary | none | refused until probed |

Read-only grants for Discuss use `read_file`, never `--add-dir`. A Work turn's
`--add-dir` gives the repository edits and also loads its hooks. See D2.

Hidden reads render as `read_file(<path>)` denies on every capability. The
staged agent folder (`<cwd>/.agents/`) and the client folder get
`write_file` denies, just as Claude denies edits of the client directory.

### Decoder

```text
init                         -> session(conversation_id)
step_update agent_response   -> message(text_delta) [+ usage when DONE, + status]
step_update tool             -> raw trace
step_update subagent_info    -> status(subagent)
result status=SUCCESS        -> answer(response)
result status!=SUCCESS       -> error(error or finished_reason)
result denied_actions        -> error naming the refused action (D1)
non-JSON line                -> raw
```

`init` is the first line of each process, and its `conversation_id` is the
session. A `result` with `status: SUCCESS` and an empty `response` that carries
`denied_actions` is an error, not an empty answer.

## Containment story

Capability is fixed in code (invariant 4). Each capability renders one policy
object. Both the argv and the prompt's write-boundary text derive from that
object.

- **File tools are bounded.** The workspace is the stage cwd plus admitted
  roots. Other roots get `write_file` allows, protected paths get denies, and
  anything else is soft-denied. All of this was probed (P8).
- **The Work shell is unbounded**, as with Claude and OpenCode.
  `canonical_state_boundary` is `file_tools_only`. The agy sandbox is off for
  Work, because it would cut the network and devices that real compute needs.
  This is the same reasoning as Claude's.
- **Discuss has no general shell.** Unlisted commands are soft-denied.
- **Graph authority stays with the stage** (invariant 4b). Nothing is parsed
  from answers.
- **Gaps that agy adds:**
  - hooks run outside every rule (D2);
  - shell reads are never hidden (P11), so every launch reports `unhidden`;
  - user plugins and skills load on every launch.

## Session and stage handling

- The stage cwd is outside Git, as for every provider. agy's path rules are
  absolute, so unlike OpenCode no Git guard is needed.
- The staged agent file is written into `<stage>/.agents/agents/` before each
  launch, including on resume. Its name is a digest of its content.
- Resume passes `--conversation <checkpoint>`. A fresh start passes nothing and
  records the `init` `conversation_id`. Invariant 10g holds: there is no silent
  fallback.
- Fork is refused (D4).
- Turn end: the local turn and the remote fence both count results against
  inputs. A non-`SUCCESS` result is terminal. Process exit after stdin closes
  is the backstop. Exit 3 with `AGY_ERROR` is a provider error.
- Steering uses the `queue` behavior. A follow-up is delivered when written, and
  its result is the next `result` in order. RCP closes stdin when the results
  catch up with the inputs.

## Failure and auth liveness

- **Readiness:** `agy --version`, then `agy models`. Exit 0 with rows means
  worth launching. It is a presence check only.
- **Liveness:** `agy -p /usage --output-format json`. A `status: SUCCESS`
  proves the token works against the server, at zero model cost. This is what
  "verify sign-in" runs. A failure shows "Sign in with `agy`" on the card.
- **Turn errors:** an `AGY_ERROR` line is classified by its status field. Quota
  wording reaches `classify_terminal_error`'s existing markers. Revoked-login
  matching stays False until a real revoked login is captured.
- **Login persistence is unproven.** The login lives under
  `~/.gemini/antigravity-cli/`, reached through `HOME` (P19). The changelog
  says macOS sign-in also uses the system keychain, and oversized tokens fall
  back to a private secrets file. Before v1 ships: confirm which store a
  personal account uses, how long a token lives, and that a team server account
  stays signed in across reboots and logouts. See D3.

## Picker behavior

The registry lists `agy` everywhere, and readiness only annotates it
(memory: pickers list every supported provider). Model choices come from
`agy models`. With an unreadable catalog the list is empty and a saved model is
still offered as typed. There is no effort control, because `reasoning` is
empty for every slug. The machine card hides the shell-timeout and auto-compact
fields for agy, driven by profile attributes, never an id check. The logo needs
an entry in `web/src/projects/ProviderMark.tsx` plus an SVG. Without one, the
label shows as text.

## Generic changes outside the package

Adding agy would add a fourth set of id branches. Each one moves into a profile
attribute in the same PR:

| Today | Change |
| --- | --- |
| `agents/provider_environment.py:374`, `agents/hidden_read.py:504`: `if provider == "opencode"` | `profile.native_tools_hide_reads` (OpenCode False, agy False) |
| `agents/staged_hidden_read.py:86`, `api/space_machines.py:273`: three login files | `profile.login_files`, rendered on the host |
| `runs/agent_threads.py:537`: OpenCode has no session summary | `profile.supports_session_summary` |
| Shell timeout assumed by every profile (`base.py:435`, `space_machines.py`) | `profile.shell_timeout_supported`; the card omits the field when it is False |
| `runs/space_chat.py:134`: `provider or "codex"` | `DEFAULT_PROVIDER` |
| `tests/test_providers.py:394`, `tests/test_hidden_read_capability_invariant.py:111`: exact id lists | iterate `PROVIDER_IDS` |
| `web/src/projects/ProviderMark.tsx:4` | add `agy` logo |

Codex-only receipts (`codex_resume`, `codex_fresh_session_retry`) and the
`*_roots` config fields stay as they are, because agy has no session format.

## Tests and verification

Test-first, scoped to the change:

1. **Decoder unit tests** from recorded probe lines (fixtures under
   `tests/fixtures/agy/`, scrubbed): session, answer, error, `denied_actions`,
   per-step usage, cumulative `result.usage` ignored, and an `init` that names
   no tools.
2. **Argv and policy tests per capability:**
   - forbidden flags never appear;
   - all three permission lists are always present;
   - Discuss never gets `--add-dir`;
   - Work's `write_file` allows equal `scope.writable_roots` minus the workspace;
   - protected and hidden denies are present;
   - the agent digest is stable.
3. **Fence tests:** shared vectors run through the local turn and the remote
   fence, including two queued inputs and an error result.
4. **Registry tests** iterate `PROVIDER_IDS` with agy included: remote bundle,
   catalog fallback, readiness version floor.
5. **Real-turn checks** (live, not in CI) on throwaway data, as in the live
   agent turn recipe, with real `agy` 1.5.1, local and on one SSH Linux host:
   - Discuss: reads a read dir. A file write outside is soft-denied, and RCP
     shows a clear error.
   - Work: writes a repo root. A protected path fails. The shell can reach the
     network.
   - Resume keeps the conversation.
   - A steer queues a second turn.
   - Skills menu.
   - Model picker.
   - Sign-ins card with `/usage` verify.
   - A PATH shim counts the real `agy` invocations.
6. **Probes still owed before build:**
   - session summary with an empty tool list (D5);
   - `work_like_probe_command`;
   - the Linux behavior of every row above;
   - `checkpoint` step fields;
   - a captured revoked-login `AGY_ERROR`;
   - whether `--effort` with an effort slug errors.

## Open decisions

### D1. A denied action ends the turn

In headless mode an action no rule allows stops the run with an empty reply.
Discuss denies the general shell, and models reach for `grep` and `cat` by
habit (P12). So Discuss turns would often end with nothing.

- **(Recommended) Leave the shell out of the Discuss tool list unless a client
  or browser grant needs it.** The model then cannot call it. When it is
  present, render `denied_actions` as a visible error ("Antigravity stopped: it
  needed shell access Discuss does not grant"). The prompt tells the model to
  use only the client command.
- Generate an explicit RE2 deny rule that matches every command except the
  allowed prefixes, so a denial becomes a tool error and the turn goes on.
  agy is written in Go, and RE2 has no lookahead, so the complement regex is
  machine-built and fragile.
- Give Discuss the agy sandbox with `command(*)`. Reads are then bounded by the
  sandbox, but it is no longer RCP's Discuss contract.

### D2. Hooks cannot be disabled

Global, plugin, cwd and `--add-dir` `.agents/hooks.json` commands run on every
launch, outside every rule. Claude and OpenCode both turn hooks off.

- **(Recommended) Accept the gap, and document it beside the unbounded Work
  shell.** Discuss never uses `--add-dir`, so only the member's own global and
  plugin hooks reach Discuss. The provider readiness card notes it on hover.
- Refuse to launch agy when any reachable `hooks.json` exists. Detection is
  incomplete, because plugin hooks live in plugin folders.
- Run agy with a private config home, which would drop hooks, plugins and skills
  together. It needs a separate login per home (D3) and loses the member's own
  skills.

### D3. Sign-in and its persistence

The login is found through `HOME` and may also use the keychain. Redirecting
`HOME` signs the CLI out (P19).

- **(Recommended) v1 uses the CLI's own login only, like OpenCode.** RCP
  verifies it with `/usage` and shows `agy` as the sign-in command. Before v1,
  verify persistence across reboots on macOS and on a Linux team server.
- Build an RCP-managed `auth.py` with per-login homes now. It is blocked on
  learning whether a token in a redirected home survives without the keychain.

### D4. Branching a chat from an agy session

agy has no fork.

- **(Recommended) A branch of an agy chat starts a fresh agy session, with the
  existing tail-transcript fallback, and records the fresh start visibly.**
- Refuse "Branch this chat" for agy sources.

### D5. Session summary

Until an empty agent tool list is proven to mean "no tools", agy refuses
`session_summary`, as OpenCode does. The provider-switch tail fallback covers
it.
