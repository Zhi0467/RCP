"""Lifecycle explorers: every stored machine with bug history, checked to a bound.

Six machines have an explorer (handoff, agent task, Experiment episode with
watchers, agent schedules, nightly consolidation, Proposals with approval
cards). ``engine.py`` explores them against real code; ``catalog.py`` writes
down every other stored machine; ``test_writers.py`` keeps each owner's writer
list complete. All of them run in PR CI.

Bounds. A machine's bound is at least its horizon plus one, at least the
length of every Safety or Step trace its witnesses need, and at least the
depth of every suspected stuck state plus the horizon. Its exploration and
witnesses stay within 20 s locally; the ``hang_guard`` in ``conftest.py``
fails a test past 120 s. When the minimum does not fit, the machine first
drops member actions no rule or trace needs, then lowers its horizon to 2,
and only then its bound, turning each trace it no longer reaches into a plain
regression test and saying so in its docstring. Each test asserts its exact
state count, so a view change names the new count.

Witnesses. A rule gets one only where it is free: a seam that reverts a real
fix, or a seeded bad state, never a copied old function body. A Safety or
Step witness replays its trace with the patch and asserts that rule reports;
a Goal witness reruns the exploration under the patch. Rules read engine state
directly, never through a function a witness stubs.

Defects. A failure an explorer reports is confirmed by a plain regression test
from its trace and fixed at its root in the same change. Where code and spec
disagree, the fix follows the spec unless the spec is wrong, and the change
says which. A suspected defect the bound cannot reach is reported where the
change is reviewed.

Out of scope: remote stages and SSH (beyond the episode's patched outage),
races between threads or inside one function, the UI, and provider behavior.
Each action is atomic.
"""
