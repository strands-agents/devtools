"""Evaluator that assesses whether agent responses are appropriately concise.

This evaluator uses an LLM judge to rate response conciseness on a scale from
"Extremely verbose" to "Too brief", with "Appropriately concise" being the ideal.
"""

from enum import Enum

from pydantic import BaseModel, Field
from strands import Agent
from strands.models.model import Model
from typing_extensions import TypeVar, Union

from strands_evals.types.evaluation import EvaluationData, EvaluationOutput
from strands_evals.types.trace import EvaluationLevel, TextContent, ToolExecution, TraceLevelInput
from strands_evals.evaluators.evaluator import Evaluator

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


CONCISENESS_SYSTEM_PROMPT = """You are an objective judge evaluating whether an AI assistant's response is appropriately concise. Your task is to assess if the response communicates the necessary information efficiently without unnecessary verbosity or excessive brevity.

# Evaluation Guidelines:
Rate the conciseness of the assistant's response using this scale:

1. Extremely verbose
- Contains extensive unnecessary repetition
- Includes large amounts of irrelevant tangents
- Uses excessive filler phrases and padding
- Response is 3x+ longer than needed

2. Too verbose
- Contains noticeable unnecessary repetition
- Includes some irrelevant information
- Could be significantly shortened without losing meaning
- Response is 1.5-3x longer than needed

3. Slightly verbose
- Contains minor unnecessary padding
- A few sentences could be trimmed
- Mostly efficient but with room for improvement

4. Appropriately concise
- Contains all necessary information
- No unnecessary repetition or padding
- Well-structured and efficient
- Professional and direct without being curt

5. Too brief
- Missing important context or details
- Responses feel incomplete or rushed
- User would need to ask follow-up questions for essential information

IMPORTANT: Focus on response length relative to the information needed. A longer response for a complex topic can still be "Appropriately concise" if all content is necessary. Conversely, a short response can be "Too verbose" if it pads simple information."""


class ConcisenessScore(str, Enum):
    """Categorical conciseness ratings."""

    EXTREMELY_VERBOSE = "Extremely verbose"
    TOO_VERBOSE = "Too verbose"
    SLIGHTLY_VERBOSE = "Slightly verbose"
    APPROPRIATELY_CONCISE = "Appropriately concise"
    TOO_BRIEF = "Too brief"


class ConcisenessRating(BaseModel):
    """Structured output for conciseness evaluation."""

    reasoning: str = Field(description="Step by step reasoning to derive the final score")
    score: ConcisenessScore = Field(description="Categorical conciseness rating")


class ConciseResponseEvaluator(Evaluator[InputT, OutputT]):
    """Evaluates whether agent responses are appropriately concise."""

    evaluation_level = EvaluationLevel.TRACE_LEVEL

    _score_mapping = {
        ConcisenessScore.EXTREMELY_VERBOSE: 0.0,
        ConcisenessScore.TOO_VERBOSE: 0.25,
        ConcisenessScore.SLIGHTLY_VERBOSE: 0.5,
        ConcisenessScore.APPROPRIATELY_CONCISE: 1.0,
        ConcisenessScore.TOO_BRIEF: 0.5,
    }

    def __init__(
        self,
        model: Union[Model, str, None] = None,
        system_prompt: str | None = None,
        pass_threshold: float = 0.5,
    ):
        """Initialize the evaluator.
        
        Args:
            model: LLM model to use for evaluation
            system_prompt: Custom system prompt (defaults to CONCISENESS_SYSTEM_PROMPT)
            pass_threshold: Minimum score to pass (default 0.5)
        """
        super().__init__()
        self.system_prompt = system_prompt if system_prompt is not None else CONCISENESS_SYSTEM_PROMPT
        self.model = model
        self.pass_threshold = pass_threshold

    def evaluate(self, evaluation_case: EvaluationData[InputT, OutputT]) -> list[EvaluationOutput]:
        """Evaluate conciseness of the agent's response."""
        parsed_input = self._get_last_turn(evaluation_case)
        prompt = self._format_prompt(parsed_input)
        evaluator_agent = Agent(model=self.model, system_prompt=self.system_prompt, callback_handler=None)
        rating = evaluator_agent.structured_output(ConcisenessRating, prompt)
        normalized_score = self._score_mapping[rating.score]
        result = EvaluationOutput(
            score=normalized_score,
            test_pass=normalized_score >= self.pass_threshold,
            reason=rating.reasoning,
            label=rating.score.value,
        )
        return [result]

    async def evaluate_async(self, evaluation_case: EvaluationData[InputT, OutputT]) -> list[EvaluationOutput]:
        """Async evaluation."""
        parsed_input = self._get_last_turn(evaluation_case)
        prompt = self._format_prompt(parsed_input)
        evaluator_agent = Agent(model=self.model, system_prompt=self.system_prompt, callback_handler=None)
        rating = await evaluator_agent.structured_output_async(ConcisenessRating, prompt)
        normalized_score = self._score_mapping[rating.score]
        result = EvaluationOutput(
            score=normalized_score,
            test_pass=normalized_score >= self.pass_threshold,
            reason=rating.reasoning,
            label=rating.score.value,
        )
        return [result]

    def _get_last_turn(self, evaluation_case: EvaluationData[InputT, OutputT]) -> TraceLevelInput:
        """Extract the most recent turn from the conversation for evaluation."""
        parsed_inputs = self._parse_trajectory(evaluation_case)
        if not parsed_inputs:
            raise ValueError(
                "No turn-level inputs could be parsed from the trajectory. "
                "Ensure actual_trajectory is a Session with at least one AgentInvocationSpan."
            )
        return parsed_inputs[-1]

    def _extract_user_prompt(self, parsed_input: TraceLevelInput) -> str:
        """Extract user prompt from last message in session history."""
        if not parsed_input.session_history:
            return ""

        last_msg = parsed_input.session_history[-1]
        if not isinstance(last_msg, list) and self._has_text_content(last_msg):
            first_content = last_msg.content[0]
            if isinstance(first_content, TextContent):
                return first_content.text

        return ""

    def _format_prompt(self, parsed_input: TraceLevelInput) -> str:
        """Format evaluation prompt from parsed trace data."""
        parts = []

        if parsed_input.session_history:
            history_lines = []
            for msg in parsed_input.session_history:
                if isinstance(msg, list) and msg and isinstance(msg[0], ToolExecution):
                    continue
                if not isinstance(msg, list) and self._has_text_content(msg):
                    first_content = msg.content[0]
                    if isinstance(first_content, TextContent):
                        history_lines.append(f"{msg.role.value.capitalize()}: {first_content.text}")
            history_str = "\n".join(history_lines)
            parts.append(f"# Previous turns:\n{history_str}")

        user_prompt = self._extract_user_prompt(parsed_input)
        parts.append(f"# Target turn to evaluate:\nUser: {user_prompt}\nAssistant: {parsed_input.agent_response.text}")

        return "\n\n".join(parts)
