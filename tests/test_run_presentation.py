"""One display value per run: the generic episode and its Experiment refinement agree."""

from __future__ import annotations

import pytest

from rcp.api.episodes import serialize_episode
from rcp.api.experiment_controls import ExperimentControlResponse, _experiment_control_response
from rcp.core.models import Experiment, GraphState
from rcp.run_presentation import RunPresentation
from rcp.storage import AppStore
from rcp.storage.models import ExperimentLoopRuntime
from tests.test_episode_storage import _episode

NODE = "experiment-node"
EPISODE = "run"


def _shown(value: RunPresentation) -> tuple[str, str, str, str, bool]:
    return value.phase, value.label, value.tone, value.section, value.needs_you


def _run(
    tmp_path,
    *,
    status: str,
    ending: str | None = None,
    node_status: str = "running",
    used: int = 1,
    ceiling: int = 3,
    **runtime: object,
) -> tuple[RunPresentation, ExperimentControlResponse]:
    """One Experiment episode in ``status``, as the generic and the Experiment owner show it."""
    store = AppStore(tmp_path / "app.sqlite3")
    store.create_episode(_episode(store, EPISODE, control_node_id=NODE, ceiling=ceiling))
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status=?, ending=?, ended_at=?, wrapup_state=? WHERE episode_id=?",
            (
                status,
                ending,
                store.now() if ending else None,
                {None: "not_started", "stopped": "skipped"}.get(ending, "failed"),
                EPISODE,
            ),
        )
    episode = store.episode(EPISODE)
    assert episode is not None
    generic = serialize_episode(store, "project", episode)
    node = Experiment(
        id=NODE, type="experiment", title="Sweep", objective="Test it.", status=node_status
    )
    control = _experiment_control_response(
        GraphState(nodes={NODE: node}),
        NODE,
        ExperimentLoopRuntime(
            episode_id=EPISODE,
            invocations_used=used,
            invocation_ceiling=ceiling,
            session_bound=True,
            **runtime,
        ),
        generic,
    )
    assert control.presentation is not None
    assert control.episode is not None
    assert control.episode.presentation == control.presentation
    return generic.presentation, control


@pytest.mark.parametrize(
    ("episode", "runtime", "phase", "section"),
    [
        (
            {"status": "running"},
            {"task_active": True, "current_status": "running", "episode_live": True},
            "running",
            "running",
        ),
        ({"status": "needs_action", "ending": "exhausted", "used": 3}, {}, "needs_you", "ended"),
        ({"status": "needs_action", "ending": "human_pause"}, {}, "needs_you", "ended"),
        ({"status": "completed", "ending": "completed"}, {}, "ended", "ended"),
        (
            {"status": "stopped", "ending": "stopped"},
            {"stop_requested": True, "stop_settled": True},
            "ended",
            "ended",
        ),
    ],
)
def test_experiment_and_generic_episode_show_a_shared_state_alike(
    tmp_path, episode: dict, runtime: dict, phase: str, section: str
) -> None:
    generic, control = _run(tmp_path, **episode, **runtime)

    assert control.presentation is not None
    assert _shown(generic) == _shown(control.presentation)
    assert (generic.phase, generic.section) == (phase, section)
    assert generic.needs_you == (phase == "needs_you")


def test_paused_at_limit_is_actionable_and_distinct_from_needing_action(tmp_path) -> None:
    paused, _ = _run(tmp_path / "a", status="needs_action", ending="exhausted", used=3)
    asked, _ = _run(tmp_path / "b", status="needs_action", ending="human_pause")

    assert paused.tone == asked.tone == "actionable"
    assert paused.label != asked.label


def test_watcher_states_refine_the_generic_running_episode(tmp_path) -> None:
    generic, control = _run(
        tmp_path, status="running", episode_live=True, active=True, detached_work_active=True
    )

    assert control.health == "waiting_on_watchers"
    assert control.presentation is not None
    assert generic.tone == "running"
    assert control.presentation.tone == "waiting"
    assert control.presentation.label != generic.label
    assert control.presentation.phase == "running"


def test_closed_node_after_stop_shows_completed_only_on_the_experiment(tmp_path) -> None:
    generic, control = _run(
        tmp_path,
        status="stopped",
        ending="stopped",
        node_status="completed",
        stop_requested=True,
        stop_settled=True,
    )

    assert control.presentation is not None
    assert generic.tone == "stopped"
    assert control.presentation.tone == "completed"
    assert control.presentation.phase == generic.phase == "ended"


def test_actions_name_the_exact_records_they_act_on(tmp_path) -> None:
    _, running = _run(
        tmp_path / "a",
        status="running",
        task_active=True,
        current_status="running",
        episode_live=True,
    )
    assert running.presentation is not None
    stop = [action for action in running.presentation.actions if action.kind == "stop"]
    assert [action.episode_id for action in stop] == [EPISODE]

    _, ended = _run(tmp_path / "b", status="completed", ending="completed", node_status="proposed")
    assert ended.presentation is not None
    assert "stop" not in {action.kind for action in ended.presentation.actions}
    start = [action for action in ended.presentation.actions if action.kind == "start"]
    assert [action.node_id for action in start] == ([NODE] if ended.can_start else [])


def test_an_experiment_without_a_run_has_no_presentation() -> None:
    node = Experiment(id=NODE, type="experiment", title="Sweep", objective="Test it.")
    control = _experiment_control_response(
        GraphState(nodes={NODE: node}), NODE, ExperimentLoopRuntime(), None
    )

    assert control.presentation is None
