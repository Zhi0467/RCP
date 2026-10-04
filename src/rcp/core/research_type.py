"""The research project type: how the research ontology answers kernel questions."""

from __future__ import annotations

from rcp.core.project_types import ProjectType

RESEARCH = ProjectType(
    name="research",
    node_types=("research_question", "hypothesis", "decision", "experiment", "evidence", "blocker"),
    labels={
        "research_question": "ResearchQuestion",
        "hypothesis": "Hypothesis",
        "decision": "Decision",
        "experiment": "Experiment",
        "evidence": "Evidence",
        "blocker": "Blocker",
    },
    plural_labels={
        "research_question": "ResearchQuestions",
        "hypothesis": "Hypotheses",
        "decision": "Decisions",
        "experiment": "Experiments",
        "evidence": "Evidence",
        "blocker": "Blockers",
    },
    protected_belief_types=frozenset({"research_question", "hypothesis"}),
    belief_types=frozenset({"hypothesis"}),
    outcome_types=frozenset({"evidence"}),
    belief_outcome_relations=frozenset(
        {"supports", "weakens", "refutes", "inconclusive", "contradicts"}
    ),
    control_node_types=frozenset({"experiment"}),
    chooser_types=frozenset({"decision"}),
    blocker_types=frozenset({"blocker"}),
    question_types=frozenset({"research_question"}),
    protected_relations=frozenset({"has_subquestion", "has_hypothesis"}),
    blocking_relations=frozenset({"blocked_by"}),
    lifecycle_fields={"evidence": "validity"},
)
