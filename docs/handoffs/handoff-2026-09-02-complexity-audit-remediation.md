# Complexity audit remediation handoff

Date: 2026-09-02
Human-confirmed: 2026-09-02
Status: active. The team/server surface freeze decision is recorded and the
archived audit fixes the accepted scope. B's background-policy matrix and D's
documentation admission rules are implemented and pass their focused and
required pull-request checks. S and W remain and are being implemented
concurrently on this pull request. Findings 2 and 3 are excluded and will be
carried by a separate deployment-model pull request.

The settled decisions are to freeze new team/server surface until the active lab
handoff closes, complete the ordered storage-migration boundary, create one
reducer-owned project-session boundary, pin the background-engine policy matrix
without redesigning the engine, and tighten admission of global invariants.

This handoff closes only when S, W, B, and D each meet the closure condition
below, preserve their stated boundary, and pass their packet checks plus the
required pull-request checks. Archive it in the same change that records those
facts. Findings 2 and 3 do not block this closure.

## Scope boundary

The accepted source is the
[complexity and brittleness audit](../archive/handoffs/rcp-complexity-brittleness-audit-2026-09-02.md).
This handoff carries findings 4, 5, 6, and 7. Finding 1 is recorded in the
[team/server surface freeze decision](../decisions/2026-09-02-freeze-new-team-server-surface-until-lab-closure.md).

Do not restructure source update and restore as a second control plane or extract
the installed-service lifecycle from `create_app` here. Those are findings 2 and
3, deferred to a separate deployment-model pull request. The audit's "Complex
mechanisms that should remain" list is outside every packet.

## S — Ordered storage startup

Owner files:

- `src/rcp/storage/`
- `tests/test_storage.py`
- `tests/test_server_upgrade.py`
- `tests/fixtures/server_upgrade/`

Behavior-preservation boundary: keep one SQLite file and one public `AppStore`;
do not add an ORM or independent store services. Preserve direct upgrade support
for every promised persistence era, with shape inspection confined to the
migration that owns that historical shape.

Closure condition: new-database creation and the ordered migration runner are the
only startup paths that mutate schema or migration-owned data. After migrations,
a read-only validator checks the current schema and invariants. Every distinct
promised persistence era has a frozen database and backup fixture, and its
migration identity and order are explicit.

Checks:

```bash
uv run pytest tests/test_storage.py tests/test_server_upgrade.py -q
```

## W — Reducer-owned project session

Owner files:

- `web/src/App.tsx`
- one new project-session hook module under `web/src/`
- `web/tests/`

Behavior-preservation boundary: the backend remains the owner of lifecycle and
transition projections. Fetch effects may remain ordinary hooks; chats, tasks,
history, graph selection, and visual components keep their concrete owners. Do
not introduce Redux, a generic global store, or a client-side copy of backend
lifecycle rules.

Closure condition: canonical snapshot application and cached-tab restoration
are each one reducer transition; project-session serialization and restoration
have behavioral round-trip tests; and `App` no longer commits one project
revision through unrelated setter calls.

Checks:

```bash
npm --prefix web run build
npm --prefix web test
```

## B — Background task policy matrix

Owner files:

- `tests/test_background_policy_matrix.py`
- existing test helpers under `tests/` as needed
- the `BackgroundAgentTasks` structural rule in `AGENTS.md`

Behavior-preservation boundary: pin current observable behavior across the
existing task families. Do not change `src/rcp/background.py`, create a registry
or plugin API, hide named owner coupling behind callbacks, or move policy merely
because the engine is large.

Closure condition: one `pytest.mark.parametrize` matrix covers Start, Resume,
Retry, startup recovery, and graph repair for the existing reachable task
families, states any real-provider omissions, uses shared wait helpers, and pins
the current special rules. The engine admission rule rejects new task-kind,
patch-kind, or request-subtype branches unless they remove an exception or are
proved universal to task-row construction; structural work is triggered when a
feature must edit three or more engine entry points.

Checks:

```bash
uv run pytest tests/test_background_policy_matrix.py tests/test_background.py -q
```

## D — Global-invariant admission

Owner files:

- `AGENTS.md`
- `tests/test_agent_instructions.py`

Behavior-preservation boundary: retain the existing authority hierarchy,
numbered invariant registry, concrete policy owners, and the literal documented
line ceiling. Do not add a documentation layer or generated policy registry.

Closure condition: `AGENTS.md` says that a new global invariant must replace or
consolidate an existing global rule, name its concrete code owner, and cite an
executable test or acceptance scenario. The file remains within its enforced
180–230-line bounds.

Checks:

```bash
uv run pytest tests/test_agent_instructions.py tests/test_documentation.py -q
```

## Required pull-request checks

```bash
uv run pytest tests/test_background_policy_matrix.py tests/test_background.py tests/test_agent_instructions.py tests/test_documentation.py -q
uv run ruff check src tests packaging web/src-tauri/scripts
uv run pre-commit run --all-files
```
