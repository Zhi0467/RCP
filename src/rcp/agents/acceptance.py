from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from rcp.agents.launcher import (
    AgentEvent,
    AgentLauncher,
    AgentProcessControl,
    ProviderReadiness,
)
from rcp.limits import ACCEPTANCE_AGENT_JOB_SECONDS
from rcp.providers import AgentCapability, profile_for

ACCEPTANCE_GENERIC_WATCHER_MARKER = "[RCP acceptance: generic watchers]"

_STATE_FILE = ".rcp-acceptance-agent.json"
_JOBS_DIRECTORY = "acceptance-agent-jobs"


@dataclass(frozen=True)
class AcceptanceLaunchRecord:
    scenario: Literal["experiment_loop", "generic_watchers", "unsupported"]
    action: Literal["initial", "watch_correction", "wake", "unsupported", "remote_rejected"]
    cwd: str
    session_id: str
    watcher_count: int


class AcceptanceAgentLauncher(AgentLauncher):
    """Explicit, local-only provider double for served acceptance scenarios.

    This launcher is selected only by ``create_app(acceptance_agent=True)``. It
    never calls a provider, network service, scheduler, or GPU. Its state and
    detached job artifacts live in the persistent chat scratch directory so a
    server restart exercises the same recovery path as a real provider session.
    """

    def __init__(self) -> None:
        super().__init__()
        self._records_lock = threading.Lock()
        self._launch_records: list[AcceptanceLaunchRecord] = []

    @property
    def launch_records(self) -> tuple[AcceptanceLaunchRecord, ...]:
        with self._records_lock:
            return tuple(self._launch_records)

    def readiness(
        self,
        provider: str,
        *,
        host: str = "",
        binary: str | None = None,
        refresh: bool = False,
    ) -> ProviderReadiness:
        del binary, refresh
        profile = profile_for(provider)
        if host:
            return ProviderReadiness(
                provider=provider,
                label=f"Acceptance {profile.label}",
                installed=False,
                authenticated=False,
                version="acceptance-local-only",
                path_state="unreachable",
                reason=(
                    "Acceptance-agent mode is local-only and refuses to impersonate "
                    f"a provider on {host}."
                ),
                models=list(profile.declared),
            )
        return ProviderReadiness(
            provider=provider,
            label=f"Acceptance {profile.label}",
            installed=True,
            authenticated=True,
            version="acceptance-1",
            path_state="resolved",
            models=list(profile.declared),
        )

    async def stream(
        self,
        provider: str,
        prompt: str,
        *,
        cwd: Path,
        model: str | None = None,
        reasoning: str | None = None,
        session_id: str | None = None,
        read_dirs: list[Path] | None = None,
        write_dirs: list[Path] | None = None,
        host: str = "",
        control: AgentProcessControl | None = None,
        remote_pid_file: str | None = None,
        capability: AgentCapability,
        binary: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        del model, reasoning, read_dirs, write_dirs, remote_pid_file, capability, binary
        resolved_cwd = cwd.resolve()
        stable_session = session_id or str(
            uuid5(NAMESPACE_URL, f"rcp-acceptance-session:{resolved_cwd}")
        )
        if host:
            self._record(
                AcceptanceLaunchRecord(
                    scenario="unsupported",
                    action="remote_rejected",
                    cwd=str(resolved_cwd),
                    session_id=stable_session,
                    watcher_count=0,
                )
            )
            yield AgentEvent(
                event="error",
                text=(
                    f"Acceptance-agent mode is local-only and cannot run fixture work on {host}."
                ),
            )
            return
        if control is not None and control.pause_requested.is_set():
            yield AgentEvent(event="paused", text="Paused before acceptance fixture work started.")
            return

        state = _read_state(resolved_cwd)
        contract = _read_launch_contract(prompt)
        scenario = _scenario(prompt, contract, state)
        action = _action(contract, state)
        watcher_count = 0

        yield AgentEvent(event="session", session_id=stable_session)
        if scenario == "unsupported":
            self._record(
                AcceptanceLaunchRecord(
                    scenario=scenario,
                    action="unsupported",
                    cwd=str(resolved_cwd),
                    session_id=stable_session,
                    watcher_count=0,
                )
            )
            yield AgentEvent(
                event="error",
                text=(
                    "The acceptance agent only runs an Experiment-loop invocation or an "
                    f"ordinary Work turn containing {ACCEPTANCE_GENERIC_WATCHER_MARKER!r}."
                ),
            )
            return

        if action == "initial":
            focused_experiment_id = _focused_experiment_id(contract)
            _start_fixture_jobs(resolved_cwd)
            state = {
                "scenario": scenario,
                "focused_experiment_id": focused_experiment_id,
                "jobs_started": True,
                "watch_corrected": False,
            }
            _write_state(resolved_cwd, state)
            # Deliberately invalid. Production orchestration must retain this
            # native session and request exactly one watcher-only correction.
            _write_json(resolved_cwd / "watch.json", {"invalid": "correction required"})
            answer = "Started two deterministic CPU-only fixture jobs."
        elif action == "watch_correction":
            specs = _watch_specs(resolved_cwd)
            watcher_count = len(specs)
            _write_json(resolved_cwd / "watch.json", specs)
            state["watch_corrected"] = True
            _write_state(resolved_cwd, state)
            answer = "Corrected the watcher handoff without resubmitting either fixture job."
        else:
            if not _fixture_jobs_complete(resolved_cwd):
                yield AgentEvent(
                    event="error",
                    text="Acceptance fixture watcher woke before both detached jobs completed.",
                )
                return
            if scenario == "experiment_loop":
                focused_experiment_id = state.get("focused_experiment_id")
                if not isinstance(focused_experiment_id, str) or not focused_experiment_id:
                    raise ValueError(
                        "Acceptance Experiment state has no persisted focused Experiment id."
                    )
                tested_hypothesis_id = _tested_hypothesis_id(
                    contract,
                    focused_experiment_id,
                )
                _write_json(resolved_cwd / "watch.json", [])
                _write_json(
                    resolved_cwd / "patch.json",
                    _completion_patch(focused_experiment_id, tested_hypothesis_id),
                )
                answer = "Inspected both fixture jobs and completed the control Experiment."
            else:
                answer = "Inspected both completed fixture jobs; no graph Patch was needed."
            state["completed"] = True
            _write_state(resolved_cwd, state)

        self._record(
            AcceptanceLaunchRecord(
                scenario=scenario,
                action=action,
                cwd=str(resolved_cwd),
                session_id=stable_session,
                watcher_count=watcher_count,
            )
        )
        yield AgentEvent(event="answer", text=answer)
        yield AgentEvent(
            event="provider_exit",
            text=json.dumps(
                {
                    "return_code": 0,
                    "event_counts": {"session": 1, "answer": 1},
                    "explicit_terminal_event": True,
                    "acceptance_agent": True,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        yield AgentEvent(event="done")

    def _record(self, record: AcceptanceLaunchRecord) -> None:
        with self._records_lock:
            self._launch_records.append(record)


def _read_launch_contract(prompt: str) -> str:
    lines = prompt.splitlines()
    if len(lines) < 2:
        raise ValueError("Acceptance-agent launch text has no contract path.")
    path = Path(lines[1].strip())
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Acceptance-agent contract is unreadable: {exc}") from exc


def _read_state(cwd: Path) -> dict[str, object]:
    path = cwd / _STATE_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Acceptance fixture state is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Acceptance fixture state must be a JSON object.")
    return value


def _scenario(
    prompt: str,
    contract: str,
    state: dict[str, object],
) -> Literal["experiment_loop", "generic_watchers", "unsupported"]:
    persisted = state.get("scenario")
    if persisted in {"experiment_loop", "generic_watchers"}:
        return persisted
    if "# RCP Experiment-loop task contract" in contract:
        return "experiment_loop"
    if ACCEPTANCE_GENERIC_WATCHER_MARKER in prompt or ACCEPTANCE_GENERIC_WATCHER_MARKER in contract:
        return "generic_watchers"
    return "unsupported"


def _action(
    contract: str,
    state: dict[str, object],
) -> Literal["initial", "watch_correction", "wake"]:
    first_line = contract.partition("\n")[0].lower()
    if "watch correction" in first_line or "watcher correction" in first_line:
        return "watch_correction"
    if state.get("jobs_started"):
        return "wake"
    return "initial"


def _focused_experiment_id(contract: str) -> str | None:
    prefix = "- Focused Experiment id: `"
    for line in contract.splitlines():
        if line.startswith(prefix) and line.endswith("`"):
            return line[len(prefix) : -1]
    return None


def _tested_hypothesis_id(contract: str, focused_experiment_id: str) -> str:
    prefix = "- Current graph, including the Experiment's attempts: `"
    graph_path: Path | None = None
    for line in contract.splitlines():
        if line.startswith(prefix) and line.endswith("`"):
            graph_path = Path(line[len(prefix) : -1])
            break
    if graph_path is None:
        raise ValueError("Acceptance Experiment wake contract has no current graph path.")
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Acceptance Experiment graph is unreadable: {exc}") from exc
    if not isinstance(graph, dict) or not isinstance(graph.get("edges"), dict):
        raise ValueError("Acceptance Experiment graph has no edge map.")
    matches = [
        edge.get("target")
        for edge in graph["edges"].values()
        if isinstance(edge, dict)
        and edge.get("source") == focused_experiment_id
        and edge.get("relation") == "tests"
        and isinstance(edge.get("target"), str)
    ]
    if len(matches) != 1:
        raise ValueError("Acceptance Experiment fixture must have exactly one tested Hypothesis.")
    return matches[0]


def _start_fixture_jobs(cwd: Path) -> None:
    jobs = cwd / _JOBS_DIRECTORY
    jobs.mkdir(parents=True, exist_ok=True)
    for name in ("job-one", "job-two"):
        done_path = jobs / f"{name}.done"
        status_path = jobs / f"{name}.status"
        log_path = jobs / f"{name}.log"
        if done_path.exists() or status_path.exists() or log_path.exists():
            raise ValueError(f"Acceptance fixture job {name} already has persistent artifacts.")
        status_path.write_text("running\n", encoding="utf-8")
        log_path.write_text(f"{name}: started\n", encoding="utf-8")
        code = (
            "import pathlib,sys,time\n"
            "done,status,log,name,delay=sys.argv[1:]\n"
            "time.sleep(float(delay))\n"
            "pathlib.Path(log).open('a', encoding='utf-8').write(f'{name}: completed\\n')\n"
            "pathlib.Path(status).write_text('completed\\n', encoding='utf-8')\n"
            "pathlib.Path(done).write_text('done\\n', encoding='utf-8')\n"
        )
        subprocess.Popen(  # noqa: S603 - fixed interpreter and internal fixture payload
            [
                sys.executable,
                "-c",
                code,
                str(done_path),
                str(status_path),
                str(log_path),
                name,
                str(ACCEPTANCE_AGENT_JOB_SECONDS),
            ],
            cwd=jobs,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _watch_specs(cwd: Path) -> list[dict[str, str]]:
    jobs = cwd / _JOBS_DIRECTORY
    return [
        {
            "check_command": f"test -f {str(jobs / f'{name}.done')!r}",
            "log_path": str(jobs / f"{name}.log"),
            "cwd": str(jobs),
        }
        for name in ("job-one", "job-two")
    ]


def _fixture_jobs_complete(cwd: Path) -> bool:
    jobs = cwd / _JOBS_DIRECTORY
    return all((jobs / f"{name}.done").is_file() for name in ("job-one", "job-two"))


def _completion_patch(
    focused_experiment_id: str,
    tested_hypothesis_id: str,
) -> dict[str, object]:
    evidence_id = "ev/acceptance-result"
    evidence_edge_id = "edge/acceptance-supports"
    return {
        "summary": "Completed the deterministic acceptance Experiment with supporting evidence.",
        "ops": [
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": focused_experiment_id,
                        "changes": {"status": "completed"},
                    }
                ],
            },
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": evidence_id,
                        "type": "evidence",
                        "title": "Acceptance jobs completed",
                        "observation": (
                            "Both deterministic CPU-only acceptance jobs reached their "
                            "completed status and wrote their expected logs."
                        ),
                        "interpretation": (
                            "The bounded control loop delivered and inspected both watcher "
                            "completions."
                        ),
                        "strength": "supporting",
                        "validity": "valid",
                        "origin": "internal_run",
                    }
                ],
            },
            {
                "op": "create_edges",
                "edges": [
                    {
                        "id": "edge/acceptance-produces",
                        "source": focused_experiment_id,
                        "target": evidence_id,
                        "relation": "produces",
                        "explanation": "The acceptance Experiment produced the fixture result.",
                    },
                    {
                        "id": evidence_edge_id,
                        "source": evidence_id,
                        "target": tested_hypothesis_id,
                        "relation": "supports",
                        "explanation": (
                            "The completed watcher sequence supports the fixture Hypothesis."
                        ),
                    },
                ],
            },
            {
                "op": "create_proposals",
                "proposals": [
                    {
                        "id": "prop/acceptance-result",
                        "title": "Accept the acceptance-loop result",
                        "card": {
                            "situation_cold": (
                                "Both deterministic acceptance jobs completed and their "
                                "watchers were delivered."
                            ),
                            "why_human_now": (
                                "Only a human may accept the resulting Hypothesis status change."
                            ),
                            "consequences": (
                                "Accepting marks the tested fixture Hypothesis as supported."
                            ),
                            "decision_needed": "Approve or reject the supported status.",
                        },
                        "ops": [
                            {
                                "op": "update_nodes",
                                "nodes": [
                                    {
                                        "id": tested_hypothesis_id,
                                        "changes": {"status": "supported"},
                                        "cause": {
                                            "kind": "evidence_edge",
                                            "ref_id": evidence_edge_id,
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        ],
        "repositories_read": [],
        "change_summary": [
            "Completed the control Experiment after both acceptance jobs finished.",
            "Recorded the fixture result as supporting evidence for the tested Hypothesis.",
            "Proposed marking the tested fixture Hypothesis as supported.",
        ],
    }


def _write_state(cwd: Path, value: dict[str, object]) -> None:
    _write_json(cwd / _STATE_FILE, value)


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
