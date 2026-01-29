"""Langfuse session mapper for fetching traces and converting to Session format."""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from langfuse import Langfuse
from strands_evals.types.trace import (
    AgentInvocationSpan,
    AssistantMessage,
    InferenceSpan,
    Session,
    SpanInfo,
    TextContent,
    ToolCall,
    ToolCallContent,
    ToolExecutionSpan,
    ToolResult,
    ToolResultContent,
    Trace,
    UserMessage,
)

from .session_mapper import SessionMapper

logger = logging.getLogger(__name__)


class LangfuseSessionMapper(SessionMapper):
    """Fetches traces from Langfuse and converts to Session format for evaluation.

    This mapper enables post-hoc evaluation of agent runs by:
    1. Querying Langfuse for traces by session_id or trace_id
    2. Converting Langfuse observations to strands_evals Session/Trace format
    """

    def __init__(
        self,
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
    ):
        """Initialize the Langfuse session mapper.

        Args:
            public_key: Langfuse public key (defaults to LANGFUSE_PUBLIC_KEY env var)
            secret_key: Langfuse secret key (defaults to LANGFUSE_SECRET_KEY env var)
            host: Langfuse host URL (defaults to LANGFUSE_HOST env var or cloud.langfuse.com)
        """
        self.client = Langfuse(
            public_key=public_key or os.environ.get("LANGFUSE_PUBLIC_KEY"),
            secret_key=secret_key or os.environ.get("LANGFUSE_SECRET_KEY"),
            host=host or os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            timeout=30,  # Increase timeout for API calls
        )

    def get_session(
        self,
        session_id: str,
        limit: int = 100,
        max_retries: int = 6,
        initial_delay: float = 2.0,
    ) -> Session:
        """Fetch traces by session_id and convert to Session format.

        Uses polling with exponential backoff to handle Langfuse ingestion latency.
        Traces may not be immediately available after being sent via OTLP.

        Args:
            session_id: The session.id used when creating the agent traces
            limit: Maximum number of traces to fetch
            max_retries: Maximum number of retry attempts (default 6 = ~2 min total)
            initial_delay: Initial delay in seconds between retries (doubles each retry)

        Returns:
            Session object with all traces for the given session_id
        """
        delay = initial_delay

        for attempt in range(max_retries + 1):
            logger.debug(f"Fetching traces for session_id={session_id} (attempt {attempt + 1}/{max_retries + 1})")

            traces_response = self.client.api.trace.list(
                session_id=session_id,
                limit=limit,
            )

            logger.debug(f"Langfuse API returned {len(traces_response.data)} traces")

            if traces_response.data:
                # Traces found - convert and check for spans
                for i, trace_data in enumerate(traces_response.data):
                    logger.debug(f"  Trace {i}: id={trace_data.id}, session_id={trace_data.session_id}")

                traces: list[Trace] = []
                for trace_data in traces_response.data:
                    trace = self._convert_trace(trace_data, session_id)
                    logger.debug(f"Converted trace {trace_data.id}: {len(trace.spans)} spans")
                    if trace.spans:
                        traces.append(trace)

                # If we have traces with spans, sort chronologically and return
                if traces:
                    # Sort traces by earliest span start_time (oldest first)
                    traces.sort(
                        key=lambda t: min(
                            (s.span_info.start_time for s in t.spans),
                            default=datetime.min.replace(tzinfo=timezone.utc)
                        )
                    )
                    logger.debug(f"Final result: {len(traces)} traces with spans (sorted chronologically)")
                    return Session(traces=traces, session_id=session_id)

                # Traces found but no spans yet - observations still ingesting
                logger.debug(f"Traces found but no spans yet, observations may still be ingesting")

            # No usable traces found - retry with backoff if attempts remain
            if attempt < max_retries:
                reason = "no traces" if not traces_response.data else "traces have no spans"
                logger.debug(f"Retry: {reason}, waiting {delay:.1f}s...")
                time.sleep(delay)
                delay = min(delay * 2, 30.0)  # Cap at 30 seconds

        # Max retries exhausted
        logger.warning(f"No traces with spans found for session_id={session_id} after {max_retries + 1} attempts")
        return Session(traces=[], session_id=session_id)

    def get_session_by_trace_id(self, trace_id: str) -> Session:
        """Fetch a single trace by ID and convert to Session format.

        Args:
            trace_id: The Langfuse trace ID

        Returns:
            Session object containing the single trace
        """
        trace_data = self.client.api.trace.get(trace_id)
        session_id = trace_data.session_id or trace_id

        trace = self._convert_trace(trace_data, session_id)
        return Session(traces=[trace] if trace.spans else [], session_id=session_id)

    def _convert_trace(self, trace_data: Any, session_id: str) -> Trace:
        """Convert a Langfuse trace to strands_evals Trace format.
        
        Creates a single AgentInvocationSpan per trace, with session_history
        populated from GENERATION observations. This matches what strands_evals
        evaluators expect.
        """
        trace_id = trace_data.id

        # Get all observations for this trace
        observations = self.client.api.observations.get_many(
            trace_id=trace_id,
            limit=100,
        )

        # Sort observations by start_time to maintain conversation order
        sorted_obs = sorted(
            observations.data,
            key=lambda o: getattr(o, "start_time", datetime.min) or datetime.min,
        )

        # Collect inference spans and tool spans
        inference_spans: list[InferenceSpan] = []
        tool_spans: list[ToolExecutionSpan] = []
        
        # Track user prompt and agent response from trace-level data
        user_prompt = ""
        agent_response = ""

        for obs in sorted_obs:
            try:
                obs_type = getattr(obs, "type", None)
                obs_name = getattr(obs, "name", "")
                
                # GENERATION = LLM inference call
                if obs_type == "GENERATION":
                    span_info = self._create_span_info(obs, session_id, trace_id)
                    inference_span = self._convert_to_inference_span(obs, span_info)
                    inference_spans.append(inference_span)
                    
                    # Extract user prompt from first generation's input
                    if not user_prompt:
                        input_data = getattr(obs, "input", None)
                        user_prompt = self._extract_user_prompt_text(input_data)
                    
                    # Extract agent response from last generation's output
                    output_data = getattr(obs, "output", None)
                    response_text = self._extract_response_text(output_data)
                    if response_text:
                        agent_response = response_text
                        
                # Tool executions (type=TOOL or name contains "tool")
                elif obs_type == "TOOL" or (obs_name and "tool" in obs_name.lower()):
                    span_info = self._create_span_info(obs, session_id, trace_id)
                    tool_span = self._convert_to_tool_execution_span(obs, span_info)
                    tool_spans.append(tool_span)
                    
            except Exception as e:
                logger.warning(f"Failed to convert observation {obs.id}: {e}")

        # Build session_history from inference spans
        session_history: list[UserMessage | AssistantMessage] = []
        for inf_span in inference_spans:
            session_history.extend(inf_span.messages)

        # Create single AgentInvocationSpan representing the whole trace
        spans: list[InferenceSpan | ToolExecutionSpan | AgentInvocationSpan] = []
        
        if user_prompt or agent_response or session_history:
            # Use trace-level timestamps for the agent invocation span
            start_time = getattr(sorted_obs[0], "start_time", None) if sorted_obs else datetime.now(timezone.utc)
            end_time = getattr(sorted_obs[-1], "end_time", None) if sorted_obs else start_time
            
            agent_span_info = SpanInfo(
                trace_id=trace_id,
                span_id=f"{trace_id}_agent",
                session_id=session_id,
                parent_span_id=None,
                start_time=start_time if isinstance(start_time, datetime) else datetime.fromisoformat(str(start_time)),
                end_time=end_time if isinstance(end_time, datetime) else datetime.fromisoformat(str(end_time)),
            )
            
            agent_span = AgentInvocationSpan(
                span_info=agent_span_info,
                user_prompt=user_prompt,
                agent_response=agent_response,
                available_tools=[],  # TODO: Extract from trace metadata if available
            )
            spans.append(agent_span)
        
        # Also include inference and tool spans for detailed analysis
        spans.extend(inference_spans)
        spans.extend(tool_spans)

        logger.debug(f"Converted trace {trace_id}: 1 agent span, {len(inference_spans)} inference, {len(tool_spans)} tool")
        return Trace(spans=spans, trace_id=trace_id, session_id=session_id)
    
    def _extract_user_prompt_text(self, input_data: Any) -> str:
        """Extract the LAST user prompt text from input data.
        
        In multi-turn conversations, the input contains the full conversation history.
        We want the last user message, which is the one that triggered this agent turn.
        """
        if isinstance(input_data, list):
            # Collect all user messages, then return the last one
            last_user_content = ""
            for msg in input_data:
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        last_user_content = content
                    elif isinstance(content, list) and content:
                        first = content[0]
                        if isinstance(first, dict) and "text" in first:
                            last_user_content = first["text"]
                        elif isinstance(first, str):
                            last_user_content = first
            return last_user_content
        elif isinstance(input_data, dict):
            return input_data.get("content", input_data.get("text", input_data.get("prompt", "")))
        elif isinstance(input_data, str):
            return input_data
        return ""
    
    def _extract_response_text(self, output_data: Any) -> str:
        """Extract response text from output data.
        
        Handles multiple Langfuse output formats:
        - {"content": "..."} - standard format
        - {"text": "..."} - alternative format  
        - {"message": "..."} - Bedrock/OTLP format
        - {"response": "..."} - legacy format
        - "string" - raw string
        """
        if isinstance(output_data, dict):
            # Check common keys in order of preference
            for key in ["content", "text", "message", "response"]:
                value = output_data.get(key)
                if value:
                    if isinstance(value, str):
                        return value
                    elif isinstance(value, dict):
                        # Nested content (e.g., {"message": {"content": "..."}})
                        return value.get("content", value.get("text", str(value)))
            return ""
        elif isinstance(output_data, str):
            return output_data
        return ""

    def _create_span_info(self, obs: Any, session_id: str, trace_id: str) -> SpanInfo:
        """Create SpanInfo from a Langfuse observation."""
        start_time = getattr(obs, "start_time", None) or datetime.now(timezone.utc)
        end_time = getattr(obs, "end_time", None) or start_time

        return SpanInfo(
            trace_id=trace_id,
            span_id=obs.id,
            session_id=session_id,
            parent_span_id=getattr(obs, "parent_observation_id", None),
            start_time=start_time if isinstance(start_time, datetime) else datetime.fromisoformat(str(start_time)),
            end_time=end_time if isinstance(end_time, datetime) else datetime.fromisoformat(str(end_time)),
        )

    def _convert_to_inference_span(self, obs: Any, span_info: SpanInfo) -> InferenceSpan:
        """Convert a GENERATION observation to InferenceSpan."""
        messages: list[UserMessage | AssistantMessage] = []

        # Extract input messages
        input_data = getattr(obs, "input", None)
        if input_data:
            user_messages = self._extract_user_messages(input_data)
            messages.extend(user_messages)

        # Extract output messages
        output_data = getattr(obs, "output", None)
        if output_data:
            assistant_messages = self._extract_assistant_messages(output_data)
            messages.extend(assistant_messages)

        return InferenceSpan(span_info=span_info, messages=messages, metadata={})

    def _extract_user_messages(self, input_data: Any) -> list[UserMessage]:
        """Extract user messages from Langfuse input data."""
        messages = []

        if isinstance(input_data, list):
            for msg in input_data:
                if isinstance(msg, dict):
                    role = msg.get("role", "")
                    content = msg.get("content", "")

                    if role == "user":
                        if isinstance(content, str):
                            messages.append(UserMessage(content=[TextContent(text=content)]))
                        elif isinstance(content, list):
                            contents = self._parse_content_list_for_user(content)
                            if contents:
                                messages.append(UserMessage(content=contents))
        elif isinstance(input_data, dict):
            # Handle single message format
            content = input_data.get("content", input_data.get("text", ""))
            if content:
                messages.append(UserMessage(content=[TextContent(text=str(content))]))
        elif isinstance(input_data, str):
            messages.append(UserMessage(content=[TextContent(text=input_data)]))

        return messages

    def _extract_assistant_messages(self, output_data: Any) -> list[AssistantMessage]:
        """Extract assistant messages from Langfuse output data.
        
        Handles multiple output formats:
        - {"content": "..."} - standard format
        - {"text": "..."} - alternative format
        - {"message": "..."} - Bedrock/OTLP format (may be JSON string)
        - "string" - raw string
        """
        messages = []

        if isinstance(output_data, dict):
            # Try to extract text content from various keys
            content = ""
            
            # Check standard keys first
            if output_data.get("content"):
                content = str(output_data["content"])
            elif output_data.get("text"):
                content = str(output_data["text"])
            elif output_data.get("message"):
                # Handle Bedrock/OTLP "message" format
                msg = output_data["message"]
                if isinstance(msg, str):
                    # Try to parse as JSON (Bedrock format)
                    try:
                        parsed = json.loads(msg)
                        content = self._extract_text_from_content_blocks(parsed)
                    except json.JSONDecodeError:
                        # Plain string
                        content = msg
                elif isinstance(msg, list):
                    content = self._extract_text_from_content_blocks(msg)
                elif isinstance(msg, dict):
                    content = msg.get("content", msg.get("text", str(msg)))
            
            tool_calls = output_data.get("tool_calls", [])

            contents: list[TextContent | ToolCallContent] = []
            if content:
                contents.append(TextContent(text=content))
            for tc in tool_calls:
                contents.append(
                    ToolCallContent(
                        name=tc.get("function", {}).get("name", tc.get("name", "")),
                        arguments=tc.get("function", {}).get("arguments", tc.get("arguments", {})),
                        tool_call_id=tc.get("id"),
                    )
                )
            if contents:
                messages.append(AssistantMessage(content=contents))

        elif isinstance(output_data, str):
            messages.append(AssistantMessage(content=[TextContent(text=output_data)]))

        return messages
    
    def _extract_text_from_content_blocks(self, blocks: list | Any) -> str:
        """Extract text from Bedrock/Claude content block format.
        
        Handles formats like:
        - [{"text": "..."}, ...]
        - [{"reasoningContent": {"reasoningText": {"text": "..."}}}, ...]
        """
        if not isinstance(blocks, list):
            return str(blocks)
        
        texts = []
        for block in blocks:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict):
                # Direct text field
                if "text" in block:
                    texts.append(block["text"])
                # Reasoning content (Claude thinking)
                elif "reasoningContent" in block:
                    reasoning = block["reasoningContent"]
                    if isinstance(reasoning, dict):
                        reasoning_text = reasoning.get("reasoningText", {})
                        if isinstance(reasoning_text, dict) and "text" in reasoning_text:
                            texts.append(reasoning_text["text"])
                # Content field
                elif "content" in block:
                    content = block["content"]
                    if isinstance(content, str):
                        texts.append(content)
                    elif isinstance(content, list):
                        texts.append(self._extract_text_from_content_blocks(content))
        
        return "\n".join(texts) if texts else ""

    def _parse_content_list_for_user(self, content_list: list) -> list[TextContent | ToolResultContent]:
        """Parse a content list for user messages."""
        result: list[TextContent | ToolResultContent] = []
        for item in content_list:
            if isinstance(item, dict):
                if "text" in item:
                    result.append(TextContent(text=item["text"]))
                elif "toolResult" in item:
                    tool_result = item["toolResult"]
                    result_text = ""
                    if "content" in tool_result:
                        content = tool_result["content"]
                        if isinstance(content, list) and content:
                            result_text = content[0].get("text", "")
                        elif isinstance(content, str):
                            result_text = content
                    result.append(
                        ToolResultContent(
                            content=result_text,
                            error=tool_result.get("error"),
                            tool_call_id=tool_result.get("toolUseId"),
                        )
                    )
            elif isinstance(item, str):
                result.append(TextContent(text=item))
        return result

    def _convert_to_tool_execution_span(self, obs: Any, span_info: SpanInfo) -> ToolExecutionSpan:
        """Convert an observation to ToolExecutionSpan."""
        input_data = getattr(obs, "input", {}) or {}
        output_data = getattr(obs, "output", {}) or {}
        metadata = getattr(obs, "metadata", {}) or {}

        tool_name = metadata.get("tool_name", getattr(obs, "name", "unknown"))
        tool_call_id = metadata.get("tool_call_id", obs.id)

        # Extract arguments
        arguments = {}
        if isinstance(input_data, dict):
            arguments = input_data.get("arguments", input_data)
        elif isinstance(input_data, str):
            arguments = {"input": input_data}

        # Extract result
        result_content = ""
        error = None
        if isinstance(output_data, dict):
            result_content = output_data.get("result", output_data.get("content", str(output_data)))
            error = output_data.get("error")
        elif isinstance(output_data, str):
            result_content = output_data

        tool_call = ToolCall(name=tool_name, arguments=arguments, tool_call_id=tool_call_id)
        tool_result = ToolResult(content=str(result_content), error=error, tool_call_id=tool_call_id)

        return ToolExecutionSpan(span_info=span_info, tool_call=tool_call, tool_result=tool_result, metadata={})
