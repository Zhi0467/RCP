# Decision records

Most rationale lives in the spec that owns the behavior, as a short "why" note.
A decision record remains only when its rationale spans several specs and would
bloat each of them. It links to the specs that own behavior and never overrides
design or specs.

- [Graph authoring and product boundaries](2026-09-05-graph-authoring-and-product-boundaries.md)
  records glossary authoring, validator advice, human graph editing and the
  explicit exclusions replacing the retired open-question register.

- [Backend structural refactor closure](2026-08-20-backend-structural-refactor-closure.md)
  records the deliberately retained engine/owner coupling, ordinary-Work fallback
  for an unproven child route, and the decision not to extract more control layers
  from `api/app.py` without measured need.
