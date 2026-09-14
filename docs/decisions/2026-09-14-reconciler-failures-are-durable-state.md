# Reconciler failures are durable state

Confirmed by the human 2026-09-14.

## What happened

The Auto-research reconciler failed to admit an episode's wrap-up because the
ending receipt exceeded its storage bound. It caught the exception, logged a
warning, returned, and repeated the identical failure on every watcher poll for
two days. The warning reached nothing: `rcp serve` writes uvicorn access lines to
the journal and nothing from RCP's own loggers. The episode stayed `wrapping_up`,
the card said "Active", the control that would have moved it on was hidden, and a
child with finished compute waited on a parent that could not finish.

## The decision

A reconciler that cannot complete a lifecycle step records that fact on the
episode it was reconciling, once, and stops repeating an attempt that cannot
change. For wrap-up that is `wrapup_state=failed` with the exception text as
`wrapup_error` and the episode settled to its ending's terminal status. The card
shows the error and the actions that remain. The receipt itself compacts to its
bound and never raises for size, so this particular failure cannot recur.

RCP's own warnings reach the journal through one stderr handler at WARNING,
emitted the first time a failure is recorded, never per poll. There is no
periodic logging.

## What this costs, stated plainly

A wrap-up marked failed does not retry on its own; a human reauthorizes or
merges. A machine-wide condition still needs its own durable record, which slice
2 of the handoff gives to a dead login.
