---
id: S01-first-project
status: implemented
tier: hermetic
driver: api + browser
covered_by:
  - tests/test_setup.py
  - tests/test_main.py
  - tests/test_web_assets.py
invariants: [1, 2, 6]
last_checked: 2026-09-01 — automated coverage passed; the complete browser journey was not redriven.
---

# Start the app and build a first graph

The plain first-run path. Nothing exists; a graph exists at the end.

The pieces are tested separately — manifest rendering, preflight, the singleton
lock, the frontend build. What no test covers is the two of them meeting: the
setup form producing a manifest that actually opens.

## Setup

An empty data directory and an empty directory to become the state repo. Fake
agent: replies, then writes a patch creating a research question, a hypothesis,
and an experiment joining them.

## Drive

1. Start the server with no project argument. The project index opens.
2. Add a project. Point it at the empty directory. Complete setup.
3. Seed it.
4. Wait for the run to finish. Open the graph.

## Assert — browser, not covered

- `project_index_lists_no_projects_initially`
- `setup_form_produces_an_openable_project` — the wizard's output is a manifest
  the app can actually open, which is the seam nothing tests
- `provider_readiness_shown_before_seeding` — you learn the CLI is missing
  before starting a run, not during
- `graph_renders_after_seed`
- `no_console_errors`

## Assert — api

- `revision == 1`
- `patch_folders == 1` — one visible `patches/batch-*`
- `no_hidden_staging_left` — no `.batch-*` survived
- `materialized_files_exist` — `graph.json`, `research.md`, `glossary.json`,
  `proposals.json`, `coverage.json`
- `graph_matches_log` — rematerializing from the log reproduces `graph.json`
- `research_md_mentions_every_node`
- `web_dist_was_built`
- `no_server_traceback`

## Failure means

The app does not start for someone who does not already have a project. The
worst possible first impression, and the one an existing-project test suite is
structurally blind to.
