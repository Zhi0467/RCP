# Agents retire graph conditions

Requested by the human 2026-09-13. This record exists because the restriction it
removes looks like a safety property, so a later reviewer is likely to restore it.

## What changed

An Experiment agent's watcher handoff may now retire a graph condition with a
stop item, the same way it retires an external observer. It could previously
retire only external observers.

## Why the old restriction had no argument

A graph condition is the same watcher mechanism as an external observer. Same
row, same delivery path, same spent invocation when it fires. Only the stop item
told them apart, so an agent that armed a condition and then stopped waiting for
that canonical fact could not withdraw it. The condition stayed live, held the
loop open, and spent an invocation on a fact nobody was waiting for.

The exclusion arrived with graph conditions themselves and no decision recorded
it. It reads as a conservative default from when conditions were new.

The human retire control is the contrast. Its predicate documents a deliberate
and much narrower scope: that control exists for a job that outlives its episode,
where Cancel would otherwise be the only move, which a condition never is. That
argument explains why the human control is not a condition's answer. It never
argued that the agent which armed one may not withdraw it.

## Why retiring a condition is the safer half

It runs no job, so nothing survives the stop and there is no "retiring is not
cancelling" ambiguity to explain. The terminal state already existed: a condition
whose node is removed retires the same way, and `active_graph_watchers` selects
on status and notified, so a stopped row simply leaves the canonical evaluation
index.

## What did not change

The human-side control still excludes an Experiment graph condition. Releasing an
*ended* episode held by a live condition stays the open question that control's
own predicate already names, and this record does not answer it. An agent retires
a condition while its episode is running; after a durable ending no agent runs.

A stop still cannot claim RCP cancelled anything, and every stop still travels in
the `external` list.

## Owner

`_validate_and_apply_agent_watcher_stops` in `src/rcp/storage/watchers.py`, with
`test_experiment_agent_retires_a_graph_condition_and_stop_list_is_atomic` in
`tests/test_graph_condition_watchers.py`. Current behavior is in
[Conversations, episodes, and watchers](../specs/conversations-episodes-and-watchers.md).
