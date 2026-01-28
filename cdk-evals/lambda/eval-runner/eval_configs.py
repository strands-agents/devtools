"""Configuration registry for evaluation types.

Maps eval_type strings to their evaluator configurations.
"""

from dataclasses import dataclass
from typing import Type
from strands_evals.evaluators import (
    Evaluator,
    HelpfulnessEvaluator,
    FaithfulnessEvaluator,
    GoalSuccessRateEvaluator,
    ToolSelectionAccuracyEvaluator,
)
from evaluators import (
    ConciseResponseEvaluator,
    ReleaseNotesStructureEvaluator,
    TurnEfficiencyEvaluator,
)


@dataclass
class EvalConfig:
    """Configuration for a specific evaluation type."""
    evaluators: list[Type[Evaluator]]
    description: str


EVAL_CONFIGS: dict[str, EvalConfig] = {
    "github_issue": EvalConfig(
        evaluators=[
            HelpfulnessEvaluator,
            GoalSuccessRateEvaluator,
            ToolSelectionAccuracyEvaluator,
            TurnEfficiencyEvaluator,
            ConciseResponseEvaluator,
        ],
        description="Evaluates github issue resolution agents"
    ),
    "release_notes": EvalConfig(
        evaluators=[
            ReleaseNotesStructureEvaluator,
            HelpfulnessEvaluator,
            ConciseResponseEvaluator,
        ],
        description="Evaluates release notes generation"
    ),
    "reviewer": EvalConfig(
        evaluators=[
            HelpfulnessEvaluator,
            GoalSuccessRateEvaluator,
            ConciseResponseEvaluator,
            TurnEfficiencyEvaluator,
        ],
        description="Evaluates code review agents"
    ),
    "implementer": EvalConfig(
        evaluators=[
            ToolSelectionAccuracyEvaluator,
            TurnEfficiencyEvaluator,
        ],
        description="Evaluates implementation agents"
    ),
}


def get_eval_config(eval_type: str) -> EvalConfig:
    """Get evaluation config for a given type.
    
    Args:
        eval_type: The type of evaluation (e.g., "github_issue", "reviewer")
        
    Returns:
        EvalConfig with evaluators and description
        
    Raises:
        ValueError: If eval_type is not recognized
    """
    config = EVAL_CONFIGS.get(eval_type)
    if not config:
        valid_types = list(EVAL_CONFIGS.keys())
        raise ValueError(f"Unknown eval_type: '{eval_type}'. Valid types: {valid_types}")
    return config
