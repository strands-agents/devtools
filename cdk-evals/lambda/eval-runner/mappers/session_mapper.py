"""Base class for session mappers that fetch traces from various sources."""

from abc import ABC, abstractmethod

from strands_evals.types.trace import AgentInvocationSpan, Session


class SessionMapper(ABC):
    """Fetches traces from a data source and converts to Session format for evaluation.

    Subclass this to implement mappers for different trace sources (Langfuse,
    CloudWatch, etc).
    """

    @abstractmethod
    def get_session(self, session_id: str) -> Session:
        """Fetch a session by session_id.

        Args:
            session_id: The session identifier

        Returns:
            Session object with all traces for the given session_id
        """
        pass

    def get_session_by_trace_id(self, trace_id: str) -> Session:
        """Fetch a session by trace_id.

        Not all data sources support this operation.

        Args:
            trace_id: The trace identifier

        Returns:
            Session object containing the trace

        Raises:
            NotImplementedError: If the mapper does not support this operation
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not support fetch by trace_id")

    @staticmethod
    def extract_input_output(session: Session) -> tuple[str, str]:
        """Extract user input and agent output from a Session.

        Walks through traces and spans looking for AgentInvocationSpan which
        contains the user_prompt and agent_response. Falls back to generic
        span input/output attributes if no AgentInvocationSpan is found.

        Args:
            session: Session object containing traces and spans

        Returns:
            Tuple of (user_input, agent_output)
        """
        user_input = ""
        agent_output = ""

        for trace in session.traces:
            for span in trace.spans:
                if isinstance(span, AgentInvocationSpan):
                    agent_output = span.agent_response or ""
                    if span.user_prompt and not user_input:
                        user_input = span.user_prompt

                if not user_input and hasattr(span, "input") and span.input:
                    user_input = str(span.input)
                if not agent_output and hasattr(span, "output") and span.output:
                    agent_output = str(span.output)

        return user_input, agent_output
