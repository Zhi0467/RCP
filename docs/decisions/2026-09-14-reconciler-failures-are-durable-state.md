# Reconciler failures are durable state

Confirmed by the human 2026-09-14, revised the same day after a design review.

## What happened

The Auto-research reconciler failed to admit an episode's wrap-up because the
ending receipt exceeded its storage bound. It caught the exception, logged a
warning, returned, and repeated the identical failure on every watcher poll for
two days. Nothing about the failure was written on the episode. The episode
stayed `wrapping_up`, the card said "Active", the control that would have moved
it on was hidden, and a child with finished compute waited on a parent that
could not finish.

Whether the warning reached the service journal is not established: Python's
last-resort handler writes warnings to stderr without configuration, and the
service's stderr routing was not inspected during the incident. What is
established is that a repeating failure had no durable record at the episode
level and no consumer.

## The decision

A reconciler that cannot complete a lifecycle step records that fact on the
episode it was reconciling, once, and stops repeating an attempt that cannot
change. The record is phase-specific:

- a permanent admission defect marks the wrap-up `failed` with the exception
  text as `wrapup_error` and settles the episode to its ending's terminal
  status; the card shows a nonblocking report error, as the specs already
  promise for report failures, and the episode's own controls stay available;
- a login blockage parks the wrap-up as `pending` with `blocked_reason=sign_in`
  and spends no report attempt; it resumes after a verified sign-in;
- a transient unavailability is retried on the next poll with its diagnostic
  receipt on the reconciling operation; it never ends the report lifecycle by
  itself.

The receipt itself is built once, compacts to its bound, is persisted at
admission, and is reused afterwards, so this particular failure cannot recur.
One warning is logged per process for each episode and failure kind and none on
repeats; the journal route was inspected and already carries warnings, at the
journal's default priority. There is no periodic logging.

## What this costs, stated plainly

A wrap-up marked failed does not retry on its own; the report is missing until a
human continues the episode, whose next ending produces its own report. A
machine-wide condition still needs its own durable record, which the login
failure handling gives to a dead account.
