"""Post-hoc evaluation runner for agent traces.

This module enables running evaluations on agent traces that have already been
captured, allowing for asynchronous evaluation workflows:
1. Agent runs complete and traces are sent to a trace store (Langfuse, CloudWatch, etc.)
2. Later, this module fetches those traces and runs evaluations
"""

import logging
from typing import Any, Callable

from strands_evals.types.trace import Session

from mappers import SessionMapper

logger = logging.getLogger(__name__)


class PostHocEvaluator:
    """Run evaluations on pre-captured traces."""

    def __init__(self, mapper: SessionMapper):
        """Initialize the post-hoc evaluator.

        Args:
            mapper: A SessionMapper instance for fetching traces from the data source
        """
        self.mapper = mapper

    def fetch_session(self, session_id: str) -> Session:
        """Fetch a session by session_id.

        Args:
            session_id: The session.id used when creating the agent traces

        Returns:
            Session object with all traces
        """
        return self.mapper.get_session(session_id)

    def fetch_session_by_trace_id(self, trace_id: str) -> Session:
        """Fetch a session by trace_id.

        Not all mappers support this operation.

        Args:
            trace_id: The trace ID

        Returns:
            Session object containing the single trace

        Raises:
            NotImplementedError: If the mapper does not support this operation
        """
        return self.mapper.get_session_by_trace_id(trace_id)

    def run_evaluators(
        self,
        session: Session,
        evaluators: list[Callable[[Session], dict[str, Any]]],
    ) -> dict[str, Any]:
        """Run a list of evaluators on a session.

        Args:
            session: The Session object to evaluate
            evaluators: List of evaluator functions that accept a Session

        Returns:
            Dictionary mapping evaluator names to their results
        """
        results = {}
        for evaluator in evaluators:
            evaluator_name = getattr(evaluator, "__name__", str(evaluator))
            try:
                result = evaluator(session)
                results[evaluator_name] = result
            except Exception as e:
                logger.error(f"Evaluator {evaluator_name} failed: {e}")
                results[evaluator_name] = {"error": str(e)}
        return results


# Example usage
if __name__ == "__main__":
    import os

    from mappers import LangfuseSessionMapper

    # Ensure environment variables are set for Langfuse
    required_vars = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        print(f"Missing environment variables: {missing}")
        print("Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY to use this example.")
        exit(1)

    # Create a mapper for the trace source
    mapper = LangfuseSessionMapper()

    # Create the evaluator with the mapper
    evaluator = PostHocEvaluator(mapper)

    # Fetch a session and print basic info
    test_session_id = os.environ.get("TEST_SESSION_ID", "test_session")
    print(f"Fetching session: {test_session_id}")

    session = evaluator.fetch_session(test_session_id)

    print(f"Found {len(session.traces)} traces")
    for trace in session.traces:
        print(f"  Trace {trace.trace_id}: {len(trace.spans)} spans")
