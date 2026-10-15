"""Who-may rules for human actions.

Each rule is a pure predicate over facts its owner already loaded. Where a
route, a storage transaction, and a ``can_*`` projection judge the same rule,
they call the same predicate. Owners keep authentication, visibility
(non-members get 404), refusal status, check order, and lifecycle admission; a
predicate only answers "may this member".

A true result never stands in for a human authorization snapshot (invariant 3),
and no agent dispatch, profile, tool, or write-root code reads this package
(invariant 4).
"""
