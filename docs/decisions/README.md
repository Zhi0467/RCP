# Active decision records

- [Backend structural refactor closure](2026-08-20-backend-structural-refactor-closure.md)
  records the deliberately retained engine/owner coupling, ordinary-Work fallback
  for an unproven child route, and the decision not to extract more control layers
  from `api/app.py` without measured need.
- [Codex app-server is a profile runtime](2026-08-25-codex-app-server-runtime.md)
  records per-profile selection, per-invocation evidence, the exact pre-prompt
  fallback boundary, and why RCP does not own a persistent provider-session
  runtime or Codex Desktop ordering.
- [Personal-to-team transfer uses one sanitized project archive](2026-08-27-personal-to-team-transfer-archive.md)
  records the sole transfer format, complete finished-history boundary, removal
  of source execution bindings, and validated atomic target import.
- [Source server uses staged releases and split operator/service privilege](2026-08-27-source-server-install-and-update-privilege.md)
  records the disposable bootstrap, clean per-commit releases, unprivileged
  source builds, and narrow root coordinator for systemd lifecycle.

Decision records explain rationale that remains materially useful for an active
migration, live tradeoff, or easy-to-regress architectural boundary. They link to
the current specification that owns behavior and never override design or specs.
Archive a decision after its rationale no longer needs active implementation
attention.
