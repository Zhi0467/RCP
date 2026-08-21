# Active decision records

- [Backend structural refactor closure](2026-08-20-backend-structural-refactor-closure.md)
  records the deliberately retained engine/owner coupling, ordinary-Work fallback
  for an unproven child route, and the decision not to extract more control layers
  from `api/app.py` without measured need.

Decision records explain rationale that remains materially useful for an active
migration, live tradeoff, or easy-to-regress architectural boundary. They link to
the current specification that owns behavior and never override design or specs.
Archive a decision after its rationale no longer needs active implementation
attention.
