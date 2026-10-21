from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents import AgentEvent
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import MergeAnswersCommandRequest
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.agents.write_scope import ProjectWriteScope, WritableRepositoryRoot
from rcp.core.authority import CapturedChangeAuthority
from rcp.core.models import (
    AuthorizedHuman,
    BranchMergeReceipt,
    GraphBranchMetadata,
    GraphState,
    Patch,
    ResearchQuestion,
)
from rcp.core.operations import UpdateNodesOperation
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history import RevisionConflict
from rcp.runs import branch_merge as merge_module
from rcp.runs import merge_contract
from rcp.runs.branch_merge import (
    BranchMergeContext,
    BranchMergeEligibility,
    BranchMergeRunOutcome,
    BranchMergeStage,
    branch_merge_provenance,
    require_graph_only_merge_scope,
    reusable_merge_answers,
    stream_branch_merge_run,
)
from rcp.runs.merge_contract import (
    FieldConflict,
    MergeAnswers,
    MergeAnswersRejected,
    MergeDiagnostic,
    MergeHiddenClash,
    MergePlan,
    MergePreparationContract,
    NodeGroupConflict,
    NodeGroupPromotion,
    PresentValue,
    ReviewCard,
    ReviewGroup,
)
from rcp.runs.patch_validator import MergeAnswersCheck
from rcp.runs.shared import _sse
from rcp.service import RunRequest
from rcp.transport import StateUnavailable


def _head(
    revision: int,
    *,
    branch_id: str | None = None,
    marker: str = "a",
) -> GraphHeadRef:
    return GraphHeadRef(
        target=(
            GraphTargetRef(kind="branch", branch_id=branch_id)
            if branch_id is not None
            else GraphTargetRef()
        ),
        revision=revision,
        transition_id=marker * 64 if revision else None,
    )


def _question(
    *,
    title: str = "Question",
    motivation: str = "Base motivation",
    status: str = "open",
    updated_rev: int = 2,
) -> ResearchQuestion:
    return ResearchQuestion(
        id="rq/merge",
        type="research_question",
        title=title,
        question="What should the project test?",
        motivation=motivation,
        status=status,
        created_rev=2,
        updated_rev=updated_rev,
    )


def _state(
    revision: int,
    *,
    question: ResearchQuestion | None = None,
    extra: ResearchQuestion | None = None,
) -> GraphState:
    nodes = {"rq/merge": question or _question(updated_rev=revision)}
    if extra is not None:
        nodes[extra.id] = extra
    return GraphState(
        revision=revision,
        project_truth_scope=["repo"],
        nodes=nodes,
    )


def _context(
    *,
    main_revision: int = 4,
    main_question: ResearchQuestion | None = None,
    branch_question: ResearchQuestion | None = None,
    branch_id: str | None = None,
    task_id: str = "merge-task",
) -> BranchMergeContext:
    branch_id = branch_id or str(uuid.uuid4())
    authorizer = AuthorizedHuman(
        space_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        display_name="Episode owner",
    )
    merge_dispatcher = AuthorizedHuman(
        space_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        display_name="Merge dispatcher",
    )
    base_head = _head(2, marker="b")
    branch_head = _head(3, branch_id=branch_id, marker="c")
    metadata = GraphBranchMetadata(
        branch_id=branch_id,
        episode_id=branch_id,
        project_id="project-one",
        base_head=base_head,
        head=branch_head,
        authorized_by=authorizer,
    )
    return BranchMergeContext.create(
        merge_task_id=task_id,
        authorized_by=merge_dispatcher,
        metadata=metadata,
        eligibility=BranchMergeEligibility(
            branch_head=branch_head,
        ),
        base_graph=_state(2),
        branch_graph=_state(
            3,
            question=branch_question or _question(motivation="Branch motivation", updated_rev=3),
        ),
        main_head=_head(main_revision, marker="d" if main_revision == 4 else "e"),
        main_graph=_state(
            main_revision,
            question=main_question or _question(updated_rev=main_revision),
        ),
        run_truth_scope=["repo"],
    )


def _local_scope(stage: BranchMergeStage, context: BranchMergeContext) -> ProjectWriteScope:
    assert stage.local_stage is not None
    return ProjectWriteScope.create(
        project_id=context.metadata.project_id,
        execution_machine="laptop",
        execution_host="",
        capability="orchestrate",
        stage_root=str(stage.local_stage),
        workspace_root=str(stage.workspace),
        repositories=[],
        protected_write_paths=[str(stage.local_stage.parent / "state" / ".research")],
    )


def _branch_source_ref(*, repository: str = "repo", record: str = "record-merge") -> dict:
    return {
        "machine": "laptop",
        "truth_repository": repository,
        "source": "codex",
        "session_id": "session-merge",
        "record_uuid": record,
        "timestamp": "2026-08-12T00:00:00Z",
        "excerpt": "The branch task recorded this source.",
    }


def test_graph_only_scope_has_no_repository_roots(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    stage = BranchMergeStage(local_stage=root, remote_stage=None, workspace=workspace)
    context = _context()
    scope = _local_scope(stage, context)

    require_graph_only_merge_scope(scope, context=context, stage=stage)
    assert scope.repository_roots == []
    assert scope.writable_roots == [str(workspace)]

    repo = tmp_path / "repo"
    repo.mkdir()
    broadened = ProjectWriteScope.create(
        project_id=context.metadata.project_id,
        execution_machine="laptop",
        execution_host="",
        capability="orchestrate",
        stage_root=str(root),
        workspace_root=str(workspace),
        repositories=[WritableRepositoryRoot(alias="repo", machine="laptop", path=str(repo))],
        protected_write_paths=[str(tmp_path / "state" / ".research")],
    )
    with pytest.raises(ValueError, match="code merge roots"):
        require_graph_only_merge_scope(broadened, context=context, stage=stage)
    # A code merge admits exactly its named roots and no others.
    require_graph_only_merge_scope(broadened, context=context, stage=stage, code_roots=[str(repo)])
    with pytest.raises(ValueError, match="code merge roots"):
        require_graph_only_merge_scope(
            broadened, context=context, stage=stage, code_roots=[str(tmp_path / "other")]
        )


class _FakeLauncher:
    def __init__(self, patch_text: str) -> None:
        self.patch_text = patch_text
        self.sessions: list[str | None] = []
        self.write_dirs: list[list[Path]] = []
        self.prompts: list[str] = []

    async def stream(
        self,
        _provider: str,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None,
        write_dirs: list[Path],
        **_kwargs: object,
    ) -> AsyncIterator[AgentEvent]:
        self.prompts.append(prompt)
        self.sessions.append(session_id)
        self.write_dirs.append(write_dirs)
        (cwd / "patch.json").write_text(self.patch_text, encoding="utf-8")
        yield AgentEvent(event="session", session_id=session_id or "native-session")
        yield AgentEvent(event="provider_exit", text='{"return_code":0}')
        yield AgentEvent(event="done")


class _SequenceLauncher(_FakeLauncher):
    def __init__(self, patch_texts: list[str]) -> None:
        super().__init__(patch_texts[0])
        self.patch_texts = patch_texts

    async def stream(
        self,
        _provider: str,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None,
        write_dirs: list[Path],
        **_kwargs: object,
    ) -> AsyncIterator[AgentEvent]:
        index = len(self.sessions)
        self.prompts.append(prompt)
        self.sessions.append(session_id)
        self.write_dirs.append(write_dirs)
        (cwd / "patch.json").write_text(self.patch_texts[index], encoding="utf-8")
        yield AgentEvent(event="session", session_id=session_id or "native-session")
        yield AgentEvent(event="provider_exit", text='{"return_code":0}')
        yield AgentEvent(event="done")


class _Run:
    """Exercise the real run/check boundary with frozen planner/preparer fakes."""

    def __init__(self, tmp_path, monkeypatch, *, conflicts=(), nonempty=True):
        self.context = _context()
        self.initial = self.context
        self.plan = _plan(self.context, conflicts=conflicts)
        self.stage = BranchMergeStage(
            local_stage=tmp_path / "merge-stage",
            remote_stage=None,
            workspace=tmp_path / "merge-stage" / "workspace",
        )
        self.stage.workspace.mkdir(parents=True)
        self.history = SimpleNamespace()
        self.checks = MergeAnswersCheck(
            plan=self.plan,
            history=self.history,
            task_id=self.plan.merge_task_id,
            turn_id="initial",
        )
        self.outcome = BranchMergeRunOutcome()
        self.launches = []
        self.preparations = []
        self.commits = []
        self.nonempty = nonempty
        self.behavior = self.answer
        self.prepare_behavior = None
        self.commit_behavior = None
        self.next_plan = None
        self.before_commit = None
        self.reconcile = None
        monkeypatch.setattr("rcp.runs.merge_patch.prepare_merge_candidate", self.prepare)
        monkeypatch.setattr("rcp.runs.merge_patch.build_merge_patch", self.build)
        monkeypatch.setattr("rcp.runs.merge_plan.plan_merge", lambda context: self.next_plan)
        monkeypatch.setattr(merge_module, "merge_planning_context", lambda *args, **kwargs: args[0])
        monkeypatch.setattr(merge_module, "commit_branch_merge_with_history", self.commit)
        monkeypatch.setattr(merge_module, "branch_merge_receipt_from_committed_patch", self.receipt)
        monkeypatch.setattr(merge_module, "_provider_turn", self.provider)

    def receipt(self, patch):
        return BranchMergeReceipt(
            outcome="committed",
            provenance=patch.branch_merge,
            authorized_by=patch.authorized_by,
            result_main_head=_head(patch.branch_merge.rebased_main_head.revision + 1, marker="f"),
        )

    def prepare(self, plan, answers, history):
        self.preparations.append((plan, answers, history))
        if self.prepare_behavior is not None:
            self.prepare_behavior(plan, answers)
        return SimpleNamespace(patch=self.build(plan, answers))

    def build(self, plan, answers):
        return plan.captured_authority.patch_template.model_copy(
            update={
                "ops": [
                    UpdateNodesOperation(
                        op="update_nodes",
                        nodes=[
                            {
                                "id": "rq/merge",
                                "changes": {"motivation": "resolved"},
                            }
                        ],
                    )
                ]
                if self.nonempty
                else [],
            }
        )

    def commit(self, history, patch, **kwargs):
        self.commits.append((patch, kwargs))
        if self.commit_behavior is not None:
            self.commit_behavior()
        return SimpleNamespace(patch=patch)

    def answer(self):
        answers = merge_contract.MergeAnswers(
            answers={
                conflict.conflict_id: {"kind": "branch"} for conflict in self.checks.plan.conflicts
            },
            cards={},
        )
        content = answers.model_dump_json().encode()
        (self.stage.workspace / "answers.json").write_bytes(content)
        return self.check(content)

    def check(self, content):
        return self.checks(
            MergeAnswersCommandRequest(
                mailbox_id="a" * 32,
                request_id=f"{len(self.launches):032x}",
                credential="b" * 64,
                verb="merge_answers",
                arguments={"action": "check", "answers": content.decode()},
            ),
            CommandTurnIdentity(
                episode_id=None,
                task_id=self.plan.merge_task_id,
                turn_id=self.checks.turn_id,
                authority="broker",
            ),
        )

    def provider(self, launcher, request, prompt, **kwargs):
        self.launches.append((prompt, kwargs))
        outcome = SimpleNamespace(
            session_id="merge-session",
            completed=True,
            failed=False,
            paused=False,
            remote_result_pending=False,
        )

        async def events():
            self.behavior()
            yield _sse(AgentEvent(event="session", session_id=outcome.session_id))

        return outcome, events()

    async def activate(self, plan):
        self.checks.set_plan(plan, turn_id=f"turn-{len(self.launches)}")
        return "client merge-answers check answers.json", "client", None

    async def run(self, **kwargs):
        kwargs.setdefault("start_check_turn", self.activate)
        return [
            frame
            async for frame in stream_branch_merge_run(
                RunRequest(provider="codex", run_on="laptop"),
                object(),
                load_context=lambda: self.context,
                main_history=self.history,
                stage=self.stage,
                write_scope=_local_scope(self.stage, self.initial),
                checks=self.checks,
                outcome=self.outcome,
                graph_read_scope=GraphReadScope(project_id="project-one", target=GraphTargetRef()),
                history_client="client",
                before_commit=self.before_commit,
                reconcile_receipt=self.reconcile,
                **kwargs,
            )
        ]

    def move(self, *, changed_conflict=False):
        self.context = BranchMergeContext.create(
            merge_task_id=self.initial.merge_task_id,
            authorized_by=self.initial.authorized_by,
            metadata=self.initial.metadata,
            eligibility=self.initial.eligibility,
            base_graph=self.initial.base_graph,
            branch_graph=self.initial.branch_graph,
            main_head=_head(5, marker="e"),
            main_graph=_state(5),
            run_truth_scope=["repo"],
        )
        conflicts = self.plan.conflicts
        if changed_conflict:
            conflicts = tuple(
                item.model_copy(update={"destination": PresentValue(value="new")})
                for item in conflicts
            )
        self.next_plan = _plan(
            self.context,
            conflicts=conflicts,
            created_at=self.plan.captured_authority.patch_template.created_at,
        )


def _field():
    return FieldConflict(
        conflict_id="rq/merge:motivation",
        collection="nodes",
        entity_id="rq/merge",
        field_name="motivation",
        base=PresentValue(value="base"),
        branch=PresentValue(value="branch"),
        destination=PresentValue(value="destination"),
        allowed_answer_kinds=("branch", "destination", "merged"),
    )


def _plan(context, *, conflicts=(), groups=(), created_at=None):
    provenance = branch_merge_provenance(context)
    patch = Patch(
        kind="work",
        author="agent",
        summary="Merge",
        ops=[],
        profile="orchestrator",
        task_id=context.merge_task_id,
        source_operation_id=context.merge_task_id,
        authorized_by=context.authorized_by,
        branch_merge=provenance,
        run_truth_scope=["repo"],
        **({"created_at": created_at} if created_at else {}),
    )
    return MergePlan(
        context_id=context.context_id,
        captured_authority=CapturedChangeAuthority(
            project_id=context.metadata.project_id,
            apply_target=context.main_head.target,
            patch_template=patch,
        ),
        merge_task_id=context.merge_task_id,
        authorized_by=context.authorized_by,
        provenance=provenance,
        base_head=context.metadata.effective_parent_head,
        source_head=context.metadata.head,
        destination_head=context.main_head,
        base_graph=context.base_graph,
        source_graph=context.branch_graph,
        destination_graph=context.main_graph,
        run_truth_scope=("repo",),
        conflicts=conflicts,
        review_groups=groups,
        preparation_contract=MergePreparationContract.model_validate(
            context.transition_contract.model_dump()
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("nonempty", [True, False])
async def test_merge_without_questions_has_no_provider_turn(tmp_path, monkeypatch, nonempty):
    run = _Run(tmp_path, monkeypatch, nonempty=nonempty)
    await run.run()
    assert run.outcome.status == ("committed" if nonempty else "noop")
    assert not run.launches
    assert len(run.commits) == int(nonempty)
    assert len(run.preparations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["checked", "missing", "changed", "wrong_file", "timeout"])
async def test_turn_requires_exact_successful_answers_without_correction(
    tmp_path, monkeypatch, mode
):
    run = _Run(tmp_path, monkeypatch, conflicts=(_field(),))

    def behavior():
        if mode == "missing":
            (run.stage.workspace / "answers.json").write_text('{"answers":{},"cards":{}}')
        elif mode == "wrong_file":
            run.answer()
            (run.stage.workspace / "answers.json").rename(run.stage.workspace / "patch.json")
        else:
            run.answer()
            if mode == "changed":
                with (run.stage.workspace / "answers.json").open("ab") as file:
                    file.write(b"\n")

    if mode == "timeout":

        def timeout(*args):
            raise TimeoutError("check unavailable")

        run.prepare_behavior = timeout
    run.behavior = behavior
    await run.run()
    assert len(run.launches) == 1
    assert bool(run.commits) == (mode == "checked")
    assert run.outcome.status == (
        "committed" if mode == "checked" else "retryable" if mode == "timeout" else "rejected"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_destination_movement_reuses_only_unchanged_answers(tmp_path, monkeypatch, changed):
    run = _Run(tmp_path, monkeypatch, conflicts=(_field(),))

    def first_turn():
        run.answer()
        if len(run.launches) == 1:
            run.move(changed_conflict=changed)

    run.behavior = first_turn
    await run.run()
    assert run.outcome.status == "committed", run.outcome.diagnostic
    assert len(run.launches) == (2 if changed else 1)
    if changed:
        assert run.launches[1][1]["required_session_id"] == "merge-session"
    assert run.commits[0][1]["expected_main_head"] == run.context.main_head
    assert run.preparations[-1][0] == run.next_plan


@pytest.mark.asyncio
async def test_movement_during_check_replans_instead_of_failing_answers(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch, conflicts=(_field(),))

    def move(plan, answers):
        if len(run.launches) == 1:
            run.move(changed_conflict=True)
            raise RevisionConflict("moved")

    run.prepare_behavior = move
    await run.run()
    assert run.outcome.status == "committed", run.outcome.diagnostic
    assert len(run.launches) == 2


@pytest.mark.asyncio
async def test_append_race_revalidates_and_obeys_rebase_bound(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch)

    def move():
        if len(run.commits) == 1:
            run.move()
            raise RevisionConflict("append moved")

    run.commit_behavior = move
    await run.run()
    assert run.outcome.status == "committed", run.outcome.diagnostic
    assert run.outcome.rebase_rounds == 1
    assert len(run.preparations) == 2


@pytest.mark.asyncio
async def test_unknown_publication_uses_canonical_receipt(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch)

    def uncertain():
        raise StateUnavailable("publication acknowledgment lost")

    run.commit_behavior = uncertain
    run.reconcile = lambda: run.receipt(run.commits[0][0])
    await run.run()
    assert run.outcome.status == "committed", run.outcome.diagnostic
    assert len(run.commits) == 1


@pytest.mark.asyncio
async def test_hidden_clash_promotes_once_and_repeated_answer_error_ends_turn(
    tmp_path, monkeypatch
):
    run = _Run(tmp_path, monkeypatch)
    diagnostic = MergeDiagnostic(
        code="coupled", message="coupled fields", related_node_ids=("rq/merge",)
    )
    promoted = _plan(
        run.context,
        conflicts=(
            NodeGroupConflict(
                conflict_id="group",
                node_ids=("rq/merge",),
                fields=(_field(),),
                base={"rq/merge": PresentValue(value={})},
                branch={"rq/merge": PresentValue(value={})},
                destination={"rq/merge": PresentValue(value={})},
                diagnostic_codes=("coupled",),
            ),
        ),
    )
    promotions = []

    def promote(plan, promotion):
        promotions.append(promotion)
        return promoted

    def reject(plan, answers):
        if not plan.conflicts:
            raise MergeHiddenClash(
                NodeGroupPromotion(node_ids=("rq/merge",), diagnostics=(diagnostic,))
            )
        raise MergeAnswersRejected((diagnostic,))

    monkeypatch.setattr("rcp.runs.merge_plan.promote_node_group", promote)
    run.prepare_behavior = reject
    await run.run()
    assert len(promotions) == 1
    assert len(run.launches) == 1
    assert not run.commits
    assert run.outcome.status == "rejected"


def test_rebase_card_reuse_binds_effect_digest():
    context = _context()
    group = ReviewGroup.model_construct(
        subject_key="review", effect_digest="a" * 64, changes=(), footprint={}
    )
    # This is a planner-output fake; only review identity is this boundary's concern.
    previous = _plan(context).model_copy(update={"review_groups": (group,)})
    card = ReviewCard(situation_cold="s", why_human_now="w", consequences="c", decision_needed="d")
    answers = MergeAnswers(answers={}, cards={"review": card})
    assert reusable_merge_answers(previous, previous, answers).cards == answers.cards
    changed = previous.model_copy(
        update={"review_groups": (group.model_copy(update={"effect_digest": "b" * 64}),)}
    )
    assert reusable_merge_answers(previous, changed, answers).cards == {}


@pytest.mark.asyncio
async def test_destination_movement_before_empty_settlement_replans(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch, nonempty=False)

    def move_once():
        if run.outcome.rebase_rounds == 0:
            run.move()

    run.before_commit = move_once
    await run.run()
    assert run.outcome.status == "noop"
    assert run.outcome.receipt.result_main_head == run.context.main_head
    assert run.outcome.rebase_rounds == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("keeps_moving", [False, True])
async def test_no_change_publication_race_rebases_without_retaining_stale_receipt(
    tmp_path, monkeypatch, keeps_moving
):
    run = _Run(tmp_path, monkeypatch, nonempty=False)
    attempts = []
    published = []

    def publish(receipt):
        attempts.append(receipt)
        if len(attempts) == 1:
            run.move()
            raise RevisionConflict("destination moved under publication lock")
        if keeps_moving:
            run.context = run.initial
            raise RevisionConflict("destination moved again under publication lock")
        published.append(receipt)
        return receipt

    await run.run(publish_receipt=publish, max_main_rebases=1)

    assert len(attempts) == 2
    assert attempts[0].result_main_head != attempts[1].result_main_head
    assert run.outcome.rebase_rounds == 1
    assert run.commits == []
    assert run.launches == []
    if keeps_moving:
        assert run.outcome.status == "retryable"
        assert run.outcome.receipt is None
        assert run.outcome.result_main_head is None
        assert published == []
    else:
        assert run.outcome.status == "noop"
        assert run.outcome.receipt == published[0]
        assert run.outcome.result_main_head == run.context.main_head


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical_outcome", [None, "no_change", "committed"])
async def test_receipt_publication_failure_requires_canonical_confirmation(
    tmp_path, monkeypatch, canonical_outcome
):
    run = _Run(tmp_path, monkeypatch, nonempty=False)
    attempted = []
    committed = run.receipt(run.plan.captured_authority.patch_template)

    def publish(receipt):
        attempted.append(receipt)
        raise OSError("Receipt publication acknowledgment lost")

    def reconcile():
        if canonical_outcome is None:
            return None
        if canonical_outcome == "committed":
            return committed
        return attempted[0]

    run.reconcile = reconcile
    await run.run(publish_receipt=publish)

    assert len(attempted) == 1
    assert run.commits == []
    if canonical_outcome is None:
        assert run.outcome.status == "retryable"
        assert run.outcome.receipt is None
        assert run.outcome.result_main_head is None
    else:
        assert run.outcome.status == ("noop" if canonical_outcome == "no_change" else "committed")
        assert run.outcome.receipt == reconcile()
        assert run.outcome.result_main_head == run.outcome.receipt.result_main_head


@pytest.mark.asyncio
async def test_repeated_movement_obeys_rebase_bound(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch)

    def always_move():
        if len(run.commits) == 1:
            run.move()
        else:
            run.context = run.initial
        raise RevisionConflict("moved again")

    run.commit_behavior = always_move
    await run.run(max_main_rebases=1)
    assert run.outcome.status == "retryable"
    assert run.outcome.receipt is None
    assert len(run.commits) == 2


@pytest.mark.asyncio
async def test_staged_plan_and_schema_are_the_enforced_contract(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch, conflicts=(_field(),))
    await run.run()
    inputs = run.stage.local_stage / "inputs"
    plan_file = next(inputs.glob("*-merge-plan-*.json"))
    schema_file = next(inputs.glob("*-merge-answers-schema-*.json"))
    assert MergePlan.model_validate_json(plan_file.read_bytes()) == run.plan
    assert json.loads(schema_file.read_text()) == MergeAnswers.model_json_schema()


@pytest.mark.asyncio
async def test_rebase_receives_rotated_check_and_history_commands(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch, conflicts=(_field(),))
    turns = []

    async def activate(plan):
        turn_id = f"turn-{len(turns)}"
        turns.append(turn_id)
        run.checks.set_plan(plan, turn_id=turn_id)
        return f"{turn_id} check", f"{turn_id} history", None

    def answer_and_move():
        run.answer()
        if len(run.launches) == 1:
            run.move(changed_conflict=True)

    run.behavior = answer_and_move
    await run.run(start_check_turn=activate)
    assert run.outcome.status == "committed", run.outcome.diagnostic
    assert turns == ["turn-0", "turn-1"]
    assert "turn-1 check" in run.launches[1][0]
    assert "turn-1 history" in run.launches[1][0]
    assert run.checks.receipts[0].turn_id == "turn-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("publication_error", [OSError, TimeoutError])
@pytest.mark.parametrize("reconciliation", ["absent", "io", "receipt"])
async def test_publication_io_reconciles_or_retries(
    tmp_path, monkeypatch, publication_error, reconciliation
):
    run = _Run(tmp_path, monkeypatch)

    def publish():
        raise publication_error("publication interrupted")

    def reconcile():
        if reconciliation == "io":
            raise OSError("receipt unavailable")
        return run.receipt(run.commits[0][0]) if reconciliation == "receipt" else None

    run.commit_behavior = publish
    run.reconcile = reconcile
    await run.run()
    assert run.outcome.status == ("committed" if reconciliation == "receipt" else "retryable")
    assert len(run.commits) == 1


@pytest.mark.asyncio
async def test_moved_check_reuses_latest_successful_answers_not_current_file(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch, conflicts=(_field(),))
    checked = MergeAnswers(answers={_field().conflict_id: {"kind": "destination"}}, cards={})

    def moved(plan, answers):
        raise RevisionConflict("destination moved")

    def answer_then_move():
        assert run.answer().status == "ok"
        assert run.check(checked.model_dump_json().encode()).status == "ok"
        run.move()
        run.prepare_behavior = moved
        assert run.answer().status == "unavailable"
        run.prepare_behavior = None

    run.behavior = answer_then_move
    await run.run()
    assert run.outcome.status == "committed", run.outcome.diagnostic
    assert len(run.launches) == 1
    assert run.preparations[-1][1] == checked


@pytest.mark.asyncio
async def test_internal_check_failure_ends_turn_without_retry_or_commit(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch, conflicts=(_field(),))

    def broken(plan, answers):
        raise RuntimeError("builder bug")

    def check_twice():
        assert run.answer().status == "ok"
        run.prepare_behavior = broken
        assert run.answer().status == "invalid"

    run.behavior = check_twice
    await run.run()
    assert run.outcome.status == "rejected"
    assert "builder bug" in run.outcome.diagnostic
    assert not run.commits
    assert len(run.launches) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_promotion_survives_rebase_and_reasks_only_changed_group(
    tmp_path, monkeypatch, changed
):
    run = _Run(tmp_path, monkeypatch, conflicts=(_field(),))
    diagnostic = MergeDiagnostic(code="coupled", message="coupled", related_node_ids=("rq/merge",))
    promotion = NodeGroupPromotion(node_ids=("rq/merge",), diagnostics=(diagnostic,))
    group = NodeGroupConflict(
        conflict_id="group",
        node_ids=promotion.node_ids,
        fields=(_field(),),
        base={"rq/merge": PresentValue(value={})},
        branch={"rq/merge": PresentValue(value={})},
        destination={"rq/merge": PresentValue(value={})},
        diagnostic_codes=("coupled",),
    )
    promotions = []
    causes = []
    original_parts = merge_module.branch_merge_rebase_parts

    def parts(**kwargs):
        causes.append(kwargs["cause"])
        return original_parts(**kwargs)

    def promote(plan, request):
        promotions.append((plan, request))
        current_group = group
        if changed and run.context.main_head != run.initial.main_head:
            current_group = group.model_copy(
                update={"destination": {"rq/merge": PresentValue(value={"motivation": "new"})}}
            )
        return _plan(
            run.context,
            conflicts=(current_group,),
            created_at=plan.captured_authority.patch_template.created_at,
        )

    def prepare(plan, answers):
        if plan.conflicts[0].kind != "node_group":
            raise MergeHiddenClash(promotion)

    def answer_and_move():
        run.answer()
        if len(run.launches) == 2:
            run.move()

    monkeypatch.setattr("rcp.runs.merge_plan.promote_node_group", promote)
    monkeypatch.setattr(merge_module, "branch_merge_rebase_parts", parts)
    run.prepare_behavior = prepare
    run.behavior = answer_and_move
    await run.run()
    assert run.outcome.status == "committed", run.outcome.diagnostic
    assert len(run.launches) == (3 if changed else 2)
    assert len(promotions) == 2
    assert promotions[1][0].destination_head == run.context.main_head
    assert causes == (["promotion", "destination_moved"] if changed else ["promotion"])
    assert run.preparations[-1][1].answers["group"].kind == "branch"


@pytest.mark.asyncio
async def test_a_recorded_promoted_turn_rebases_onto_a_moved_destination(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch, conflicts=(_field(),))
    diagnostic = MergeDiagnostic(code="coupled", message="coupled", related_node_ids=("rq/merge",))
    promotion = NodeGroupPromotion(node_ids=("rq/merge",), diagnostics=(diagnostic,))
    group = NodeGroupConflict(
        conflict_id="group",
        node_ids=promotion.node_ids,
        fields=(_field(),),
        base={"rq/merge": PresentValue(value={})},
        branch={"rq/merge": PresentValue(value={})},
        destination={"rq/merge": PresentValue(value={})},
        diagnostic_codes=("coupled",),
    )
    # The recorded turn answered the promoted plan; its answers name only that group.
    run.checks.set_plan(_plan(run.context, conflicts=(group,)), turn_id="recorded")
    run.move()
    monkeypatch.setattr(
        "rcp.runs.merge_plan.promote_node_group",
        lambda plan, _promotion: _plan(
            run.context,
            conflicts=(group,),
            created_at=plan.captured_authority.patch_template.created_at,
        ),
    )

    await run.run(
        recorded_answers=MergeAnswers(answers={"group": {"kind": "branch"}}, cards={}),
        recorded_promotions=(promotion,),
    )

    assert run.outcome.status == "committed", run.outcome.diagnostic
    assert not run.launches
    assert run.preparations[-1][1].answers["group"].kind == "branch"


def test_obsolete_promotion_is_not_reapplied_when_nodes_or_changes_disappear():
    context = _context()
    diagnostic = MergeDiagnostic(code="coupled", message="coupled")
    promotion = NodeGroupPromotion(node_ids=("rq/merge",), diagnostics=(diagnostic,))
    group = NodeGroupConflict(
        conflict_id="group",
        node_ids=promotion.node_ids,
        fields=(_field(),),
        base={},
        branch={},
        destination={},
        diagnostic_codes=("coupled",),
    )
    previous = _plan(context, conflicts=(group,))
    current = _plan(context)
    assert merge_module._promotion_still_applies(previous, current, promotion)
    removed = current.model_copy(update={"destination_graph": GraphState()})
    assert not merge_module._promotion_still_applies(previous, removed, promotion)
    unchanged = current.model_copy(update={"source_graph": current.base_graph})
    assert not merge_module._promotion_still_applies(previous, unchanged, promotion)
