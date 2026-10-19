# Ontology link-rule old-writer fixture

`ontology_links_v3.json.gz` is an immutable graph-history bundle written with
the unchanged source at `cc9ca220f5f3287d551df6587433be8e7c3061e4`, before the
ontology admission change. The upgrade regression pins its compressed SHA-256.
Do not regenerate it with current models.

The old models serialize the plain seed; the old `HistoryManager` writes the
remaining canonical records, branch metadata, and graph projections. Main has
all four retired endpoint shapes in both plain and lifecycle-v3 records, an
Evidence supersede transition, and a pending content Proposal. One branch
inherits those links and adds ordinary work and another pending Proposal; four
other branches each add one obsolete shape. The bundle also retains the seed's
stale graph projection. The regression copies the recorded bytes to disposable
storage and checks replay, cache restart, merge preparation, destination Proposal
stamps, explicit branch-only edge refusal, and canonical byte preservation.
