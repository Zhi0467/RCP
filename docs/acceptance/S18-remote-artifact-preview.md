---
id: S18-remote-artifact-preview
status: implemented
tier: remote
driver: api + browser
covered_by:
  - tests/test_transport.py::test_remote_stage_artifact_operations_are_exact_and_binary
  - tests/test_transport.py::test_remote_stage_resume_rejects_symlinked_artifact_scope
invariants: [10c, 10d]
last_passed: 2026-07-30 — live remote Codex on tianhaowang-gpu0.ucsd.edu
---

# A remote preview stays remote and temporary

An HTML or image artifact produced on the canonical SSH machine is opened
through RCP without starting a remote web server and without becoming a durable
local download.

Passed on 2026-07-30 against a disposable canonical repository and conversation
stage on `tianhaowang-gpu0.ucsd.edu`. RCP launched an authenticated remote Codex
chat, rendered its HTML and PNG, and streamed byte-exact downloads without a
local artifact copy. The availability failure was induced only after the real
host path passed, by restarting the disposable RCP process with an isolated
failing SSH shim; the user's network, SSH configuration, and host were unchanged.

## UI path

The attachment row is identical to a local one. The user does not see a remote
path, host name, tunnel command, or localhost URL. Selecting the row opens an
RCP URL; RCP reads the bounded artifact from the saved remote stage on demand.
Selecting Download streams the same remote bytes as an attachment without
retaining a local copy.

## Drive

1. Run an unauthorized chat on the remote canonical-state machine and request
   one HTML preview and one raster image.
2. Close and reopen the chat, then open both rows.
3. Download both files and compare them with the remote scratch sources.
4. Confirm the source files exist only in the remote conversation stage and no
   RCP artifact copy was retained after either request.
5. Make the host unreachable and try Open and Download again.
6. Restore the host, remove the artifact, and repeat both actions.

## Assert

- `remote_cli_accepted_flags`
- `remote_scratch_writable_and_graph_unchanged`
- `rcp_url_contains_no_remote_path`
- `bounded_ssh_read_on_demand`
- `explicit_remote_download_matches_source`
- `no_remote_http_server`
- `no_local_durable_copy`
- `host_unreachable_does_not_change_answer`
- `file_missing_does_not_change_answer`
- `both_failures_make_open_and_download_unavailable`
- `no_server_traceback`

## Failure means

Remote support depends on a URL meaningful only on the remote host, silently
downloads a supposedly temporary artifact, or lets SSH availability determine
whether the chat answer survives.
