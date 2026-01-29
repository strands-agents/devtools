"""Evaluator that measures turn efficiency in multi-turn conversations.

This evaluator counts the number of agent invocation turns and compares
against an expected number. Fewer turns indicates more efficient task completion.
"""

from typing_extensions import TypeVar

from strands_evals.types.evaluation import EvaluationData, EvaluationOutput
from strands_evals.types.trace import AgentInvocationSpan, EvaluationLevel, Session
from strands_evals.evaluators.evaluator import Evaluator

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class TurnEfficiencyEvaluator(Evaluator[InputT, OutputT]):
    """Evaluates how efficiently an agent completes tasks in multi-turn conversations.
    
    The goal is to minimize the number of turns needed to complete a task.
    Score is calculated as: expected_turns / actual_turns (capped at 1.0)
    
    A score of 1.0 means the agent completed in the expected number of turns or fewer.
    Lower scores indicate the conversation took more turns than expected.
    """

    evaluation_level = EvaluationLevel.SESSION_LEVEL

    def __init__(
        self,
        default_expected_turns: int = 3,
        pass_threshold: float = 0.5,
    ):
        """Initialize the evaluator.
        
        Args:
            default_expected_turns: Default expected turns if not in case metadata
            pass_threshold: Minimum score to pass (default 0.5)
        """
        super().__init__()
        self.default_expected_turns = default_expected_turns
        self.pass_threshold = pass_threshold

    def evaluate(self, evaluation_case: EvaluationData[InputT, OutputT]) -> list[EvaluationOutput]:
        """Evaluate turn efficiency by counting AgentInvocationSpan instances."""
        trajectory = evaluation_case.actual_trajectory
        
        if not isinstance(trajectory, Session):
            return [EvaluationOutput(
                score=0.0,
                test_pass=False,
                reason=f"Expected Session trajectory, got {type(trajectory).__name__}",
                label="error",
            )]
        
        # Count turns from trajectory spans
        turn_count = self._count_turns(trajectory)
        
        # Get expected turns from case metadata
        metadata = evaluation_case.metadata if evaluation_case.metadata else {}
        expected_turns = metadata.get("expected_turns", self.default_expected_turns)
        
        # Calculate score: expected/actual, capped at 1.0
        # Fewer turns = higher score
        if turn_count > 0:
            score = min(expected_turns / turn_count, 1.0)
        else:
            # No turns detected - could be an error or single-turn that didn't register
            score = 0.0
        
        test_pass = turn_count <= expected_turns
        
        return [EvaluationOutput(
            score=score,
            test_pass=test_pass,
            reason=f"Completed in {turn_count} turns (expected: ≤{expected_turns}). "
                   f"Efficiency score: {score:.2f}",
            label=f"{turn_count}_turns",
        )]

    async def evaluate_async(self, evaluation_case: EvaluationData[InputT, OutputT]) -> list[EvaluationOutput]:
        """Async evaluation - delegates to sync since no I/O needed."""
        return self.evaluate(evaluation_case)

    def _count_turns(self, session: Session) -> int:
        """Count AgentInvocationSpan instances across all traces in the session.
        
        Each AgentInvocationSpan represents one turn of agent invocation
        (user prompt -> agent response).
        """
        count = 0
        for trace in session.traces:
            for span in trace.spans:
                if isinstance(span, AgentInvocationSpan):
                    count += 1
        return count
