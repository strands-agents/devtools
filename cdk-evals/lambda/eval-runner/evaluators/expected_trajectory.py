"""Custom evaluator that compares actual tool usage against expected_trajectory.

This evaluator extracts tool names from the Session spans and compares them
against the expected_trajectory defined in the test case using set-based metrics.
"""

from typing import Any

from strands_evals.types.evaluation import EvaluationData, EvaluationOutput
from strands_evals.types.trace import Session, ToolExecutionSpan
from strands_evals.evaluators.evaluator import Evaluator


class ExpectedTrajectoryEvaluator(Evaluator[Any, Any]):
    """Evaluates whether the agent used the expected tools.
    
    Compares actual tool usage against expected_trajectory using set-based metrics:
    - Recall: What % of expected tools were actually used?
    - Precision: What % of used tools were expected?
    - F1 Score: Harmonic mean of recall and precision (used as main score)
    
    Does not use an LLM judge - pure Python comparison.
    """
    
    def __init__(self, pass_threshold: float = 0.5, score_type: str = "f1"):
        """Initialize the evaluator.
        
        Args:
            pass_threshold: Minimum score to pass (default 0.5)
            score_type: Which metric to use as score - "f1", "recall", or "precision"
        """
        super().__init__()
        self.pass_threshold = pass_threshold
        self.score_type = score_type
    
    def evaluate(self, evaluation_case: EvaluationData[Any, Any]) -> list[EvaluationOutput]:
        """Evaluate tool selection against expected trajectory."""
        
        # Get expected tools from test case
        expected_trajectory = evaluation_case.expected_trajectory
        if expected_trajectory is None:
            return [EvaluationOutput(
                score=1.0,
                test_pass=True,
                reason="No expected_trajectory defined in test case, skipping comparison",
                label="N/A"
            )]
        
        # Convert to set of expected tool names
        expected_tools = set(expected_trajectory) if isinstance(expected_trajectory, list) else set()
        
        # Extract actual tools from Session
        actual_tools = self._extract_tools_from_trajectory(evaluation_case.actual_trajectory)
        actual_tools_set = set(actual_tools)
        actual_tools_count = len(actual_tools)
        
        # Calculate metrics
        intersection = expected_tools & actual_tools_set
        
        # Recall: What % of expected tools were used?
        recall = len(intersection) / len(expected_tools) if expected_tools else 1.0
        
        # Precision: What % of tool calls were expected tools?
        precision = len(intersection) / len(actual_tools_set) if actual_tools_set else 0.0
        
        # F1: Harmonic mean
        f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
        
        # Select score based on score_type
        score_map = {"f1": f1, "recall": recall, "precision": precision}
        score = score_map.get(self.score_type, f1)
        
        # Build detailed reason
        missing_tools = expected_tools - actual_tools_set
        extra_tools = actual_tools_set - expected_tools
        
        reason_parts = [
            f"Expected tools: {sorted(expected_tools)}",
            f"Actual tools used: {sorted(actual_tools_set)} ({actual_tools_count} total calls)",
            f"Matching tools: {sorted(intersection)}",
            f"Missing tools: {sorted(missing_tools)}" if missing_tools else None,
            f"Unexpected tools: {sorted(extra_tools)}" if extra_tools else None,
            f"Recall: {recall:.2f} | Precision: {precision:.2f} | F1: {f1:.2f}",
        ]
        reason = " | ".join(p for p in reason_parts if p)
        
        # Determine pass/fail and label
        test_pass = score >= self.pass_threshold
        label = "PASS" if test_pass else "FAIL"
        
        return [EvaluationOutput(
            score=score,
            test_pass=test_pass,
            reason=reason,
            label=label
        )]
    
    async def evaluate_async(self, evaluation_case: EvaluationData[Any, Any]) -> list[EvaluationOutput]:
        """Async evaluation (same as sync since no LLM calls)."""
        return self.evaluate(evaluation_case)
    
    def _extract_tools_from_trajectory(self, trajectory: Session | list | None) -> list[str]:
        """Extract tool names from the trajectory.
        
        Args:
            trajectory: Session object or list of spans
            
        Returns:
            List of tool names (may contain duplicates for multiple calls)
        """
        if trajectory is None:
            return []
        
        if isinstance(trajectory, Session):
            tool_names = []
            for trace in trajectory.traces:
                for span in trace.spans:
                    if isinstance(span, ToolExecutionSpan):
                        tool_names.append(span.tool_call.name)
            return tool_names
        
        if isinstance(trajectory, list):
            # If it's already a list of strings (tool names), return as-is
            if all(isinstance(item, str) for item in trajectory):
                return trajectory
            # Otherwise try to extract from spans
            tool_names = []
            for item in trajectory:
                if hasattr(item, 'tool_call') and hasattr(item.tool_call, 'name'):
                    tool_names.append(item.tool_call.name)
            return tool_names
        
        return []
