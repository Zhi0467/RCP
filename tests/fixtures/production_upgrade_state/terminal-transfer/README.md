# Production-derived terminal transfer rows

`rows.json` retains one completed target request and its consumed upload from a
production SQLite database. A bounded, read-only transaction selected only those
two rows with `mode=ro` and `PRAGMA query_only=ON`. The extracted rows were
sanitized as raw SQLite values and JSON, without reconstruction through current
RCP models. Private source details and the transformation audit remain outside
the repository.

The serving application was build 3094. The retained source configuration was
written by RCP 0.3.4, with schema generation 1 and `rcp-transfer-v1`. Its absent
`record_schema_version`, legacy `machine_aliases`, repository `machine_alias`,
completed request phase, and consumed upload/proof states remain unchanged.
Every original row column, nested JSON key, list entry, number, enum, null, and
missing key is retained. Timestamps share one constant shift, preserving their
ordering and intervals.

Identity mappings replace UUIDs, member names, machine and repository aliases,
repository identities, and resolved paths with neutral fixture values. Opaque
manifest, archive, transition, preparation, lease, and proof digests are synthetic
and consistent wherever repeated. The source-configuration digest and the
source-release receipt's target-admission digest are recomputed from the
sanitized historical JSON shape using its canonical serialization. No protected
proof row, secret, credential, archive bytes, project history, or live filesystem
content is included.

This is a two-row regression slice, not a complete application or deployment
snapshot. Insert its row dictionaries directly into the named tables of a
disposable database. The pre-fix upload-idleness path reads the retained upload,
then rejects the historical request before looking up its protected proof. The
fixture therefore reproduces that failure without supplying proof secrets or
requiring the legacy request to satisfy current writer models. Tests that need
surrounding application state must declare that state as synthetic scaffolding.
