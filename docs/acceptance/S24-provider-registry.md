---
id: S24-provider-registry
status: implemented
tier: hermetic
driver: browser
covered_by:
  - tests/test_providers.py
  - tests/test_launcher.py::test_stream_reuses_capability_and_invalidates_it_after_launch_failure
  - tests/test_launcher.py::test_cold_readiness_does_not_block_stream_event_loop
  - tests/test_api.py::test_local_provider_warmup_starts_after_health_is_available
  - tests/test_api.py::test_project_readiness_does_not_open_or_materialize_project
  - tests/test_provider_capabilities.py
  - web/tests/providers.test.mjs
last_passed: 2026-08-01 — isolated local browser drive covered all five agent
  surfaces, live Codex per-model effort narrowing, provider reset, and forced
  re-probe; process-lifetime reuse, lazy remote discovery, invalidation, and
  launch-time reuse passed in the 455-test backend suite. All 109 web tests
  passed, with no browser-console or server errors.
invariants: [4]
---

# Every agent choice offered is a choice the provider actually accepts

The provider, model, and reasoning-effort controls offer exactly what the
selected provider supports — no invented values, no missing ones, and no control
that silently does nothing.

Two bugs motivated this scenario, both found on 2026-07-30:

- The reasoning list was **hand-typed from memory** and wrong. It offered
  `minimal`, which the current Codex models do not accept, and omitted `max` and
  `ultra`, which they do.
- The reasoning control was **hidden for Claude** on the false premise that the
  provider command dropped it. It does not — [providers.py:253](../../src/rcp/providers.py:253)
  passes `--effort`. A correct control was removed because the person removing
  it guessed instead of reading.

Both share one cause: provider knowledge lived in a frontend component, written
from memory, with no path back to the CLI that owns the truth.

## UI path — confirmed 2026-07-30

**Registering a provider is adding a profile.** One backend module owns the
registry. A profile declares the provider's id and display label, how to probe
its authentication, how to enumerate its models, and how to build its launch
command. Nothing about a provider is written anywhere else — not a `("codex",
"claude")` tuple, not a `Literal["claude", "codex"]`, not a display name
ternary, not an option list in a component.

**The catalog is read from the CLI wherever the CLI can tell us.** Codex answers
`codex debug models` with a JSON catalog carrying each model's slug, display
name, supported reasoning levels, and default level; RCP reads it and offers
exactly that. Claude Code has no equivalent, so its profile declares its lists
literally and records the CLI version they were read from. A declared list is a
maintained thing that goes stale, and it says so; a probed list cannot.

**Reasoning is per model, not per provider.** Codex's supported efforts differ
between models — `gpt-5.6-sol` accepts `ultra`, `gpt-5.5` does not. Selecting a
model narrows the effort list, and an effort no longer offered falls back to the
model's declared default rather than being sent and rejected at the API.

**Model is a picker, not a text box**, populated from the catalog. It keeps an
explicit "Provider default" entry, which is what the empty string has always
meant in the manifest.

**Provider capability belongs to the app process, not a project.** Once the
backend is healthy it warms each unique local provider target in the background.
A target is the provider, host, and exact executable path. Projects only map
their configured machines onto those targets, so opening a project never starts
provider subprocesses and projects that share a target share one result.

Successful capability remains valid for the app process lifetime. A provider
launch uses it directly instead of repeating version, authentication, and model
catalog probes. Remote targets are discovered lazily rather than contacted for
every registered project at startup. Changing a provider path or machine setting,
or observing a provider launch failure, invalidates that target.

**The refresh control always re-probes the catalog, not just readiness.** It
bypasses and replaces any app-scoped capability. Installing a new model,
authenticating, or upgrading the CLI is picked up by pressing it — no RCP restart.

The controls live where they already do: the collapsed agent-config block on
every agent surface, and the per-surface profiles in Project Settings and
project setup.

## Drive

1. Open Project Settings and expand an agent profile.
2. Select Codex. Read the model picker and the reasoning picker.
3. Select a model that supports `ultra`, then one that does not, and read the
   reasoning picker after each.
4. Select Claude. Confirm a reasoning control is present and offers Claude's own
   levels.
5. Press the readiness refresh control.
6. Repeat in project setup and in the run dialog.
7. Open a second project using the same local provider target, then launch an
   agent without pressing Refresh.

## Assert

- `no_offered_value_is_rejected` — every model and effort the UI offers is
  accepted by the provider CLI; nothing offered produces an
  `invalid_enum_value` or unsupported-model error
- `codex_catalog_is_probed_not_declared` — the Codex model and effort lists come
  from `codex debug models` at probe time, not from a literal in RCP
- `claude_declared_list_is_versioned` — Claude's declared lists record the CLI
  version they were verified against, and that version is visible to whoever
  maintains them
- `reasoning_narrows_with_model` — changing the model updates the effort list,
  and an effort the new model rejects is not left selected
- `claude_reasoning_is_offered` — Claude shows a reasoning control, because its
  command passes `--effort`
- `model_is_a_picker` — the model control is a select carrying an explicit
  provider-default entry, not a free-text input
- `switching_provider_drops_the_other_providers_model` — a model id belongs to
  one provider, so changing provider resets the model to the provider default
  rather than offering the previous provider's model under the new one
- `refresh_repopulates_the_catalog` — pressing refresh re-probes models and
  efforts, not only installed/authenticated
- `local_capabilities_warm_after_app_health` — app startup is never held behind
  a provider probe, but the unique local targets begin warming once it is usable
- `project_open_never_probes_a_provider`
- `projects_share_capability_by_provider_host_and_binary`
- `launch_reuses_process_lifetime_capability`
- `remote_capability_is_lazy`
- `settings_and_launch_failure_invalidate_capability`
- `adding_a_provider_touches_one_module` — a new provider is registered by
  adding one profile; no provider id, label, or option list exists outside the
  registry
- `unreachable_provider_degrades_quietly` — when a CLI is missing, not
  authenticated, or unreachable over SSH, the pickers fall back to the saved
  manifest values and the readiness line states the problem; the surface does
  not break or silently discard the saved choice
- `no_console_or_application_request_errors`

## Remote — verified 2026-07-30

Driven against a real host (`tianhaowang-gpu0.ucsd.edu`). Both CLIs were found
on the login-shell PATH by the existing `bash -lic` wrapper, Codex's catalog was
probed over SSH and returned the same seven models with per-model efforts, and
Claude's declared list was served. An unreachable host (`murphybox`) returned no
models and left the pickers on the saved manifest values.

Two costs measured on that host, per provider: the catalog adds a **third SSH
round trip at ~2.15s**, roughly doubling remote readiness. The app-scoped
capability registry pays that cost once per unique remote target when the target
is first needed. `provider_readiness` still ships empty in the project snapshot,
so neither local warming nor remote discovery gates first paint. `withSaved`
keeps saved values selectable while a target is unresolved.

An unreachable host takes the full 10s probe timeout and now says so rather than
claiming the CLI is not installed — the previous wording sent the human to
install software on a machine they could not log into.

## Failure means

RCP is offering the human a choice the provider will reject, hiding one it would
accept, or has grown a second place where provider facts are written from
memory.
