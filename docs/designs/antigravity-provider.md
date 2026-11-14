# Antigravity CLI (`agy`) as an RCP provider

Status: design, not built. Probed against `agy` 1.5.1 on macOS (arm64) and
1.5.8 on a Linux GPU host on 2026-11-13. Every prompt-running probe ran in a
throwaway folder, with no dangerous-permissions flag and none of the user's own
grants.

This design extends the three-provider registry
(`src/rcp/providers/__init__.py`) to a fourth profile, `agy`. Before merge its
rules move into [providers-and-containment](../specs/providers-and-containment.md),
and this file is deleted.

## Summary

`agy` fits RCP's profile shape. It has a JSONL stream, multi-turn stdin,
per-launch settings, deny-beats-allow file rules, a model catalog, a skill
inventory, and an authenticated quota call that costs zero tokens. Its
containment is close to Claude's: file tools are bounded, and the Work shell is
not.

Three facts set it apart from every existing provider:

1. **An unapproved action ends the turn.** In headless mode, a tool call that
   no rule allows is soft-denied: the run stops with an empty reply and a
   `denied_actions` list. An explicit `deny` rule is different: the tool fails
   and the turn goes on. Handled by tool lists ([D1](#d1-a-denied-action-ends-the-turn-settled)).
2. **Hooks cannot be turned off.** `hooks.json` in the global config, plugins,
   the working directory and every `--add-dir` repository runs outside every
   permission rule. Accepted ([D2](#d2-hooks-cannot-be-disabled-settled)).
3. **The user's global settings leak in.** A saved `agentMode: "auto"` silently
   approves commands. RCP overrides it on every launch. `--settings` replaces
   whole permission lists, so the user's own rules can neither widen nor narrow
   RCP's.

agy has no native fork, no shell-timeout control, and no way to route shell
commands through RCP's hidden-read wrapper. The whole login is one token file
under `HOME`, which lets agy join both sign-in pools through per-login overlay
homes ([Sign-in contract](#sign-in-contract)).

## Probe evidence

Each row was observed, not read from docs. Rows P1–P21 ran on macOS only;
[Gates before build](#gates-before-build) lists the ones that must be repeated
on Linux. Paths are placeholders.

| # | Probe | Observed |
| --- | --- | --- |
| P1 | `agy -p "…" --output-format stream-json --mode plan --sandbox --model gemini-3.8-flash-low` | Exit 0. An `init` line, `step_update`s, then one `result`. `conversation_id` is on every line. |
| P2 | Same, with `--conversation <id>` | Resumed the same id. The model saw the prior turn. |
| P2b | `--mode plan` | Sends `/plan <prompt>`. The model wrote a plan into `~/.gemini/antigravity-cli/brain/<id>/` without approval. Plan mode is a workflow, not a read-only mode. |
| P3 | `--settings` deny `read_file(<ws>/secret.txt)` | `view_file` failed with "Matches user-configured deny rule". The turn continued. Literal path only. |
| P4 | `--sandbox`, no allow rule, run `wc` | Ran unprompted. Sandboxed commands need no approval. |
| P5 | No sandbox, allow only `command(wc)`, run `cat` | **Ran**, because of the user's global `agentMode: "auto"`. |
| P6 | `--settings '{"agentMode":"default"}'` | Exit 1. The default mode is spelled `""`. |
| P6b | `agentMode:""`, allow `command(wc)`, run `wc` then `cat` | `wc` ran. `cat` was soft-denied and **the run ended**: `response: ""`, `denied_actions:[{"action":"command"}]`, exit 0. |
| P7 | Same, run `head` (allowed in the user's global list) | Soft-denied. `--settings` replaced the global allow list. |
| P8 | `--mode accept-edits`, `allowNonWorkspaceAccess:false`, allow `write_file(<extra>)`, deny `write_file(<ws>/protected)` | Workspace file written. Protected: tool error, turn continued. Extra root: written. Outside: soft-denied, turn ended. |
| P9 | `.agents/agents/rcp-probe.md` in cwd with `tools: [view_file, list_dir, grep_search]`, `--agent rcp-probe` | Loaded from an untrusted folder. The model had only the listed tools; input tokens halved. `init.tools` still lists the full static set. |
| P10 | `PreInvocation` hooks in `<cwd>/.agents/hooks.json` and `<repo>/.agents/hooks.json`, `--add-dir <repo>` | **Both ran.** |
| P11 | `SHELL=<logging wrapper>` | Never invoked. `run_command` ignores `$SHELL`. |
| P12 | Ask for `grep_search`, `list_dir` | Unavailable despite `init.tools`. The model reaches for shell `grep`. |
| P13 | `agy -p /usage --output-format json` | Zero tokens, per-bucket `remaining_fraction`. Signed out: exit 1, an OAuth URL on stderr. |
| P14 | `agy -p /skills --output-format json` | `command.data.skills[]` with `name`, `description`, `path`, `builtin`, `model_invocable`. |
| P15 | `agy models` | `slug<TAB>label` rows; the effort is in the slug. Signed out: exit 1, no browser. |
| P16 | `--model bogus` | Exit 1, a `result` with `status:"ERROR"`, valid models on stderr. |
| P17 | `--input-format stream-json`, two `{"event":"user"}` lines 2 s apart, `queuedMessages:"queue"` | Two turns, one `result` each, FIFO, one process. Inputs carry no ids. |
| P18 | Usage | Each `agent_response` step with `state:"DONE"` carries that call's `usage`. `result.usage` is cumulative over the **whole conversation**, across processes. |
| P19 | `HOME=<empty dir> agy -p …` | Signed out, and **opened a browser sign-in**. Any `-p` or turn on a signed-out home can do this. |
| P20 | `ANTIGRAVITY_GEMINI_DIR`, `ANTIGRAVITY_APP_DATA_DIR` | Do not relocate the CLI login. Only `HOME` does. |
| P21 | Binary strings | The CLI login uses a file token store, not the keychain. |
| P22 | `HOME=<throwaway> agy acp --login`, `open`/`xdg-open`/`BROWSER` shimmed to fail; macOS 1.5.1 and Linux 1.5.8 | No browser. The CLI printed one URL and read the pasted code on stdin. The home gained `.gemini/antigravity-cli/antigravity-oauth-token` (mode 600). Linux saved it with no Secret Service. |
| P23 | That home under `env -i`, fresh processes, with and without D-Bus | Signed in on both systems. |
| P24 | Move only the token file aside | Signed out without it, signed in with it. That file is the whole login. |
| P25 | `agy -p /logout` | Refused in print mode. Sign-out is interactive only. |
| P26 | An ordinary run in a real home | The token file was rewritten as the run started: the CLI refreshes in place, so the home must stay writable. |
| P27 | Overlay home: every entry symlinked except the token file | `git config --global`, `ssh -G`, a real `ssh`, `uv` and `~` resolve through the overlay. Reads only; writes were not tested. |

Docs: [headless mode](https://antigravity.google/docs/cli/headless.md),
[permissions](https://antigravity.google/docs/permissions.md),
[terminal sandbox](https://antigravity.google/docs/sandbox.md),
[settings](https://antigravity.google/docs/settings.md),
[hooks](https://antigravity.google/docs/hooks.md),
[custom agents](https://antigravity.google/docs/subagents.md), and the bundled
`~/.gemini/antigravity-cli/cache/CHANGELOG.md`.

## Contract checklist

**Native**: agy does it as RCP needs. **Adapter**: the profile translates.
**Missing**: the row names the consequence.

### Identity, install, version

| Contract | Owner | agy | Status |
| --- | --- | --- | --- |
| `id`, `label` | `base.py:398` | `agy`, "Antigravity" | Native |
| Executable | `install_paths` `base.py:418` | `~/.local/bin/agy` (one Go binary) | Native |
| Native update | `ProviderNativeUpdate` `base.py:368` | `agy update` | Native |
| Version floor | `validate_readiness_version` `base.py:478` | 1.5.1 on **every** capability, overridden as OpenCode does (`opencode/profile.py:174`): every kind relies on the probed rule semantics | Adapter |
| Dependency registry | `src/rcp/dependencies.py` | Declare `agy`: purpose, roles, macOS and Linux, optional tier | Adapter |

### Launch and wire protocol

| Contract | Owner | agy | Status |
| --- | --- | --- | --- |
| Runtime | `legacy_runtime_id`, `runtime_aliases`, `runtime_choices` | One runtime `agy.stream-json.v1`; alias `stream-json`; choice label "Antigravity stream JSON" | Native |
| argv and prompt | `ProviderProfile.command` `base.py:588` | See [Launch command](#launch-command). The prompt is one `{"event":"user","message":{"content":…}}` line; stdin stays open | Native |
| Per-launch policy | `ProviderTurn.command` | `--settings '<json>'`: session-only, bad values exit 1 | Native |
| Stream decode | `decode_event` | See [Decoder](#decoder) | Adapter |
| Turn end and fence | `ProviderTurn.receive_line`; `TurnFence` `turn_fence.py:19` | Count `user` inputs against `result` outputs; the first non-`SUCCESS` result is terminal. `AgyStreamTurnFence` in `providers/agy/remote.py`, stdlib only | Adapter |
| Errors | `probe_failure_evidence`, `credential_failure` `base.py:635` | `AGY_ERROR: {json}` on stderr with exit 3; `error:` lines with exit 1 for usage errors; `result.finished_reason` (`print_timeout`, `interrupted`, `error`) | Adapter |
| Launch degradation | `launch_degradation` `base.py:617` | Bad `--model` fails outright (P16) | Native |

### Sessions

| Contract | Owner | agy | Status |
| --- | --- | --- | --- |
| Resume | `command(session_id=…)` | `--conversation <id>` (P2) | Native |
| Fork | `fork_session_id`; `SessionFormat.prepare_session_fork` | No flag. Conversations are SQLite files | **Missing**: [D4](#d4-branching-a-chat-from-an-agy-session) |
| Session indexing | `session_format` | SQLite, not JSONL | Missing by choice, as OpenCode |
| Stale session | `classify_terminal_error` `__init__.py:133` | Not observed; stays `provider_error` | Missing, safe default |
| Native memory | spec "Native provider memory is off" | No switch. The staged tool list leaves out the knowledge tools | Adapter, injection unverified |

### Capability and containment

| Contract | Owner | agy | Status |
| --- | --- | --- | --- |
| Mode per capability | `AgentCapability` `base.py:29` | `--mode accept-edits` for every kind that writes, which is all but `session_summary`; `agentMode:""` always in `--settings`. Never `auto`, `plan` or skip-permissions | Adapter |
| Tool set | Claude `_CLAUDE_TOOLS` | Staged agent `<cwd>/.agents/agents/rcp-<digest>.md`, `--agent rcp-<digest>` (P9) | Adapter |
| Write roots | `ProviderWriteScope`; `_require_project_write_scope` `base.py:702` | Workspace = cwd plus `--add-dir` roots; `write_file(<root>)` allows other roots; `write_file(<protected>)` denies (P8) | Native |
| Work shell | `canonical_state_boundary` `base.py:613` | `command(*)`, no `--sandbox` (it cuts network and devices) | Adapter; `file_tools_only` |
| Read context for scratch kinds | spec "Claude" section | `read_file(<dir>)` allows, never `--add-dir`, which would make the folder editable and load its hooks (P10) | Adapter |
| Shell for scratch kinds | invocation-gate client | `run_command` only when the launch carries a client, and only `command(<exact client argv>)` | Adapter, [D1](#d1-a-denied-action-ends-the-turn-settled) |
| Paper coach | `_paper_write_roots` `base.py:688` | `write_file` allows for the coach roots; `command(*)` as Work | Adapter |
| Session summary | `agent_threads.py:540` | Refused by the existing rule: a summary needs an enforced hidden-read scope, and agy launches are always `unhidden` | No change |
| Hooks | — | No switch (P10) | Accepted, D2 |
| MCP, native browser, scheduling, messaging, image, marketplace tools | Claude `--tools` reasoning | Left out of the staged tool list. RCP's Playwright grant stays the one browser path | Adapter |
| Provider runtime storage | spec "Provider auth/session/cache exceptions" | `~/.gemini/antigravity-cli/brain/<conversation>/` is always writable (P2b) | Native exception |

### Hidden reads and credentials

| Contract | Owner | agy | Status |
| --- | --- | --- | --- |
| Native read denies | `HiddenReadScope` | `read_file(<path>)` denies (P3). Glob patterns and symlinked spellings are unprobed; see gates | Native for literal paths |
| Shell wrapper | Claude `CLAUDE_CODE_SHELL_PREFIX`; OpenCode `SHELL` | None (P11). Every agy launch reports `unhidden` with `provider_native_tools_uncovered` | **Missing** |
| Login credential attributes | `base.py:423-429`; `provider_cleanup.py:93` | `login_credential_files`, `login_home_credentials`, `remote_login_credential_files`, all set to the token path | Adapter |
| Machine login file | `staged_hidden_read.py:86` (stdlib, runs on the host) | `<home>/.gemini/antigravity-cli/antigravity-oauth-token` added to the host list; agy has no override variable besides `HOME` | Adapter |
| Credential failure | `credential_failure` | False until a revoked-login `AGY_ERROR` is captured | Missing, safe default |

### Login and readiness

| Contract | Owner | agy | Status |
| --- | --- | --- | --- |
| Presence | `auth_command`, `is_authenticated` | `agy models`; exit 0 means signed in (P15) | Adapter |
| Liveness | `login_probe_command` `base.py:495` | `agy -p /usage --output-format json`, zero tokens (P13) | Native |
| Sign-in | `provider_auth`, `provider_sign_in.py` | `agy acp --login` per login home: URL out, pasted code in (P22) | Adapter, new `paste_code` method |
| Sign-out | `ProviderAuthentication.sign_out` | Delete the login home's token file (P25) | Adapter |
| Work startup probe | `work_like_probe_command` | None until probed | Missing until probed |

### Models, usage, status, steering, skills

| Contract | Owner | agy | Status |
| --- | --- | --- | --- |
| Catalog | `catalog_command`, `parse_catalog` | `agy models`; one `ModelChoice(id=slug, label=label, reasoning=())` per row. Never pass `--effort`: the slug fixes it | Native |
| Usage | `decode_usage`, `usage_profile` | `agy.step.v1`: per `DONE` step, processed = `input_tokens + cache_read_tokens`, generated = `output_tokens`. Never `result.usage` (P18) | Adapter |
| Context tokens / window | `AgentStatusDelta` | Tokens from the latest main-agent step; no window reported | Adapter / Missing |
| Compaction, subagents | `compacting`, `AgentSubagent` | Not observed; decoded as raw trace until captured | Missing until observed |
| Steering | `steering_behavior` `base.py:265` | `queue`; see [Steering and replay](#steering-and-replay) | Adapter |
| Skills | `skill_probe`, `parse_skills`, `native_skill_token` | `agy -p /skills --output-format json` → `command.data.skills[]`, one JSONL line; token `/name` | Native |
| Auto-compact | `autocompact_hint` | None | Missing |
| Shell timeout | `shell_timeout_default_seconds` `base.py:435` | No lever. The attribute becomes `int \| None`; None means unsupported, as `autocompact_hint` does | Missing |

## The profile

One package, `src/rcp/providers/agy/`:

- `profile.py`: `AgyProfile`, argv, the `--settings` policy, the staged agent,
  decoder, usage and status.
- `remote.py`: `AgyStreamTurnFence`, stdlib only, shipped by `remote_bundle`.
- `auth.py`: `AgyAuthentication` and its `paste_code` sign-in driver.

Registration is one entry in `PROVIDERS`.

```text
AgyProfile
  id = "agy"; label = "Antigravity"
  install_paths = (".local/bin/agy",)
  native_update = ProviderNativeUpdate(self_update_args=("update",))
  legacy_runtime_id = "agy.stream-json.v1"; default_runtime = "stream-json"
  runtime_aliases = {"stream-json": ..., "agy.stream-json.v1": ...}
  runtime_choices = (ProviderRuntimeChoice(id="stream-json", label="Antigravity stream JSON"),)
  turn_fences = {"agy.stream-json.v1": AgyStreamTurnFence}
  usage_profile = "agy.step.v1"
  work_like_minimum_version = (1, 5, 1)
  validate_readiness_version -> floor on every capability
  session_format = None; autocompact_hint = None
  shell_timeout_default_seconds = None
  hidden_read_gaps = ("shell_wrapper",)          # new base attribute
  login_credential_files = (<token path in the login's local directory, as Codex's>,)
  login_home_credentials = (".gemini/antigravity-cli/antigravity-oauth-token",)
  remote_login_credential_files = (".rcp/provider-homes/agy/{login_id}/.gemini/antigravity-cli/antigravity-oauth-token",)
  auth_command / is_authenticated -> [agy, "models"], exit 0
  login_probe_command  -> [agy, "-p", "/usage", "--output-format", "json"]
  login_command        -> [agy, "acp", "--login"]
  authentication       -> AgyAuthentication()     # methods = ("paste_code",)
  catalog_command / parse_catalog -> [agy, "models"], tab-separated rows
  skill_probe / parse_skills      -> [agy, "-p", "/skills", "--output-format", "json"]
  project_write_enforcement_mode  -> "agy.permission-rules.v1"
  canonical_state_boundary        -> "file_tools_only"
  runtime(...)         -> _AgyStreamRuntime (steering_behavior = "queue")
```

### Launch command

```text
agy --input-format stream-json --output-format stream-json
    --model <slug>                       # when chosen
    --agent rcp-<digest>                 # staged in <cwd>/.agents/agents/
    --settings <policy json>
    --mode accept-edits                  # every kind but session_summary
    [--conversation <id>]                # resume
    [--add-dir <root> ...]               # Work-like kinds only
```

Never `-p`, `--sandbox`, `--mode auto`, `--mode plan`, `--remote-agent`,
`--remote-control` or `--dangerously-skip-permissions`. A unit test asserts that
no rendered argv contains them.

**Every agy process** RCP starts gets the browser shims: `PATH` shims that
make `open` and `xdg-open` fail, and `BROWSER` set to a failing shim. That
covers turns, probes, `/usage` and sign-in, so a signed-out home never opens a
browser on any desktop (P19).

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

All three permission lists are always written, because `--settings` replaces
each list it names (P7). `queuedMessages` and `artifactReviewPolicy` are set
explicitly so the user's saved values cannot interrupt or stall a headless turn.

| Capability | allow | Staged tools |
| --- | --- | --- |
| `work_auto`, `orchestrate` | `command(*)`, `read_file(*)`, `write_file(<root>)` per writable root outside the workspace, `read_url(*)` | file tools, `run_command`, `command_status`, `send_command_input`, `search_web`, `read_url_content`, subagent tools |
| `paper_suggest` | `command(*)`, `read_file(*)`, `write_file(<root>)` per coach root | as Work |
| `scratch_patch` (Seed, Refresh, scratch Patch) | `read_file(<dir>)` per read dir, `read_url(*)`, plus `command(<client argv>)` with a client | file tools, `search_web`, `read_url_content`, plus `run_command` with a client |
| `discuss` (the scratch artifact edit only) | `read_file(<dir>)` per read dir, `read_url(*)` | file tools, `search_web`, `read_url_content`. No shell tools |
| `session_summary` | refused by the existing rule | — |

Denies on every capability:

- each hidden read path as `read_file(<path>)`, written in both its canonical
  spelling and its spelling under the launch's `HOME`, so a symlinked overlay
  path cannot bypass it;
- `write_file(<cwd>/.agents)` and the client folder. A write to
  `.agents/hooks.json` would run commands on the next resume, so the deny must
  cover the whole folder (gate G3).

### Decoder

```text
init                         -> session(conversation_id)
step_update agent_response   -> message(text_delta) [+ usage when DONE]
step_update other            -> raw trace
result status=SUCCESS        -> answer(response)
result status!=SUCCESS       -> error(error or finished_reason)
result with denied_actions   -> error naming the refused action
non-JSON line                -> raw
```

A `SUCCESS` result with an empty `response` and `denied_actions` is an error,
never an empty answer.

### Steering and replay

- `close_input_after_initial = False`; `requires_protocol_completion = True`.
- `render_steer` writes one `{"event":"user","message":{"content":…}}` line.
  agy echoes nothing back, so a steer counts as delivered when its bytes are
  written, and its result is the next unclaimed `result` in FIFO order.
- The turn completes when results seen equal inputs sent, then RCP closes stdin.
- `adopt_recorded_inputs` takes the recorded message ids in order, so a
  replayed journal knows how many inputs the pass sent and matches results to
  them by position.
- A test replays a recorded pass with one steer and checks attribution.

## Containment

Capability is fixed in code (invariant 4). Each capability renders one policy
object, and both the argv and the prompt's write-boundary text come from it.
File tools are bounded by workspace, allows and denies (P8). The Work shell is
unbounded, as with Claude and OpenCode. Graph authority stays with the stage
(invariant 4b). agy adds three gaps: hooks run outside every rule, shell reads
are never hidden, and user plugins and skills load on every launch.

The stage cwd is outside Git. agy's rules are absolute paths, so no Git guard is
needed. The staged agent file is rewritten before every launch, resume
included. Invariant 10g holds: a fresh start passes no `--conversation` and
records the `init` id, and there is no silent fallback.

## Sign-in contract

agy joins RCP's two sign-in pools: the machine sign-in, plus added logins (each
member's own and shared ones) per machine.

**The credential.** `<HOME>/.gemini/antigravity-cli/antigravity-oauth-token`
is the whole login (P24). The CLI finds it only through `HOME` (P20) and
rewrites it on refresh (P26).

**Machine sign-in** (`login_id = ""`). The execution account's own `~`. RCP
never moves or copies that file. The card tells the member to run
`agy acp --login` on the host.

**Added logins: overlay homes.** Codex relocates one folder (`CODEX_HOME`).
agy can only relocate all of `HOME`, and `HOME` is also the home of every shell
command the agent runs. So each added login gets an overlay of the machine home
with one independent file, the token.

| | Local | Remote |
| --- | --- | --- |
| Home | `<data dir>/provider-homes/agy/<login_id>` | `~/.rcp/provider-homes/agy/<login_id>` |

The overlay reuses Codex's home mechanics instead of a copy. `prepare_home`,
the home lease, the `--run` exec wrapper and private-file migration move from
`providers/codex/homes.py` into a shared stdlib module. Each provider passes
its machine home and its independent paths: Codex passes `CODEX_HOME` and
`auth.json`, and agy passes `HOME` and the token path. Rules:

- **One independent path.** Every entry is a symlink into the machine home,
  except the folders on the way to the token, which are real folders of links.
- **No loops.** Every ancestor of the overlay itself (`.rcp`, and the data
  directory's ancestors such as `Library` on macOS) is a real folder of links
  with the branch leading back to the overlay left out.
- **`HOME` reaches only agy.** The launch runs `homes.py --run <home> -- agy …`,
  which sets `HOME` on the agy process alone. RCP's own launch prefix (git
  identity, `PATH`, pid file) resolves against the real home first.
- **Writes land in the machine home.** Shared folders the CLI would create
  (`conversations`, `brain`, config) are created in the machine home and linked
  before the first launch. A name that appears in the overlay anyway, such as a
  new top-level dotfile or a symlink replaced by an atomic rename, is migrated
  into the machine home on the next preparation with no active turn, as Codex
  migrates today.

So `--conversation` resume, skills, plugins, git identity, SSH keys and tool
caches stay shared across a login switch (P27).

**Sign-in (`paste_code`).** A new method kind beside `device_code` and
`token_entry`:

- `DeviceLogin` gains `accepts_code = False` and
  `code_input(code: str) -> bytes`. agy's driver sets `accepts_code = True` and
  returns `code + "\n"`.
- `agy acp --login` runs under the login's home with the browser shims. The
  driver reads the one `https://accounts.google.com/…` URL as a URL token and
  returns it as `verification_url`. `ProviderSignInStatus` gains
  `awaiting_code: bool`.
- `POST /api/providers/{provider}/logins/sign-in/{attempt_id}/code` with
  `{"code": …}` writes `code_input` to the process stdin once. The code never
  reaches argv, events, responses, receipts, logs or backups, as Claude's
  setup-token never does.
- `PROVIDER_SIGN_IN_TIMEOUT_SECONDS` and the existing cancel route apply
  unchanged.
- The routing checks in `api/provider_login.py:436` and
  `runs/provider_sign_in.py:275,336` treat `paste_code` like `device_code`.
- Exit 0 means the CLI accepted the code. The sign-in counts only after
  verification passes.

**Presence, verification, sign-out.** Presence is `agy models`. Verification
is `/usage`, a real authenticated call. Sign-out deletes the home's token file
under the login gate through the existing cleanup attributes.

**Concurrency.** Refresh rewrites the token, so turns and probes on one login
share the existing startup gate per `(provider, machine_id, login_id)`, as
Codex's rotating token does.

**Hidden reads.** Every agy token file is hidden from every run's native tools,
agy's own included, through the existing `login_home_credentials` glob, as
Codex's `auth.json` is. The CLI process reads its token directly.

## Generic changes outside the package

Only what agy needs to avoid new id branches:

| Today | Change |
| --- | --- |
| `base.py:435` shell timeout assumed for every profile | `shell_timeout_default_seconds: int \| None`; the machine card omits the field for None |
| No profile way to say "no shell wrapper" | `hidden_read_gaps` attribute; agy reports `provider_native_tools_uncovered` from it. OpenCode's existing branches stay as they are |
| `staged_hidden_read.py:86` host list of machine login files | add agy's path |
| `providers/codex/homes.py` | move the generic overlay into a shared module, parameterised by machine home and independent paths |
| `provider_auth.py`, `provider_sign_in.py`, `provider_login.py` | the `paste_code` method |
| `tests/test_hidden_read_capability_invariant.py:37-108` | an agy branch that reads `--settings` and the staged agent file |
| `web/src/projects/ProviderMark.tsx:4` | agy logo; without it the label shows as text |

## Tests and verification

Test-first, scoped to the change:

1. **Decoder**, from scrubbed recorded lines in `tests/fixtures/agy/`: session,
   answer, error, `denied_actions`, per-step usage, cumulative `result.usage`
   ignored.
2. **Argv and policy per capability:** forbidden flags absent; all three lists
   present; scratch kinds never get `--add-dir`; shell tools only for Work-like
   kinds and client-carrying `scratch_patch`; `accept-edits` on every writing
   kind; Work's `write_file` allows equal writable roots minus the workspace;
   both spellings of each hidden deny; the `.agents` deny; stable digest.
3. **Fence and replay:** shared vectors through the local turn and the remote
   fence, including two queued inputs, an error result, and a replay with one
   steer.
4. **Overlay homes:** no loop from any ancestor; `HOME` set only on the agy
   process; a new top-level file and an atomically renamed file migrate to the
   machine home; the lease blocks migration during a turn.
5. **Sign-in:** a fake `agy acp --login` that prints a URL and reads a code;
   the code reaches stdin once and appears in no stored record.
6. **Registry:** iterate `PROVIDER_IDS`: remote bundle, catalog fallback,
   version floor.
7. **Real drive** on throwaway data with real `agy`, local and on one SSH Linux
   host: an artifact edit in a running orchestrator's chat lands in the stage,
   and a write outside shows a clear error; a Work turn writes a repo root, a
   protected path fails, and the shell reaches the network; resume; a steer; the
   skills menu; the model picker; a full added-login sign-in with `/usage`
   verify, a turn, and a resume across a login switch; a PATH shim counting real
   `agy` invocations.

## Gates before build

Each is a probe. A failure changes the design before code starts.

- **G1. Linux semantics.** Repeat P3, P6b, P8 and P10 on the Linux host.
- **G2. Deny matching.** Whether `read_file` and `write_file` denies match a
  glob pattern, and whether they match the resolved path or the path as typed.
  If globs fail, the matching hidden reads report uncovered through
  `hidden_read_gaps`.
- **G3. Folder deny.** `write_file(<cwd>/.agents)` refuses a write to
  `<cwd>/.agents/hooks.json`.
- **G4. Agent switch on resume.** `--conversation` with a different `--agent`
  and `--settings` than the conversation started with. If agy refuses, agy
  artifact edits are admitted with `fresh_session = True` from the start, a
  recorded fresh start. The refused-resume rebuild is keyed to a Codex receipt
  and does not apply.
- **G5. Work startup probe.** Whether `--settings` with stdin closed validates
  the policy and exits.

## Decisions

### D1. A denied action ends the turn (settled)

Since one chat mode, every human chat turn is Work, which allows `command(*)`.
agy never runs a historical Discuss task, because none can be admitted for a
new provider. `discuss` reaches agy only as the scratch artifact edit
(`launch_kind="revoking"`): a comment on an artifact in a chat that refuses
human turns.

- The artifact edit's staged agent has no shell tools, so the model cannot call
  a command.
- A `scratch_patch` launch gets `run_command` only with a client, and only that
  client is allowed.
- A refusal still decodes to a visible error naming the refused action, never
  an empty answer.

Rejected: a machine-built RE2 complement deny (no lookahead, fragile), and the
agy sandbox with `command(*)` (a different contract from every other provider's
scratch kinds).

### D2. Hooks cannot be disabled (settled)

Global, plugin, cwd and `--add-dir` hooks run outside every rule. The human
accepted this. The containment spec documents it beside the unbounded Work
shell. The scratch kinds never use `--add-dir`, and the `.agents` deny keeps an
agent from planting a cwd hook.

### D3. Sign-in (settled)

Both pools, through overlay homes. See [Sign-in contract](#sign-in-contract).

### D4. Branching a chat from an agy session

agy has no fork.

- **(Recommended) Start a fresh agy session with the existing tail-transcript
  fallback, and record the fresh start visibly.**
- Refuse "Branch this chat" for agy sources.

### D5. Added logins in the first PR

Machine sign-in alone makes agy usable. Added logins need the shared overlay
module, `paste_code`, and the overlay tests, roughly half the build.

- **(Recommended) One PR with both pools**, as the human asked for parity with
  Claude and Codex, and as AGENTS.md ships design, code and docs together.
- Ship machine sign-in first, and added logins in a second PR.
