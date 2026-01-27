"""Post-hoc evaluation runner for Langfuse traces.

This module enables running evaluations on agent traces that have already been
captured in Langfuse, allowing for asynchronous evaluation workflows:
1. Agent runs complete and traces are sent to Langfuse
2. Later, this module fetches those traces and runs evaluations
"""

import logging
from typing import Any, Callable

from strands_evals.types.trace import Session

from mappers import LangfuseSessionMapper

logger = logging.getLogger(__name__)


class PostHocEvaluator:
    """Run evaluations on pre-captured Langfuse traces."""

    def __init__(
        self,
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
    ):
        """Initialize the post-hoc evaluator.

        Args:
            public_key: Langfuse public key (defaults to LANGFUSE_PUBLIC_KEY env var)
            secret_key: Langfuse secret key (defaults to LANGFUSE_SECRET_KEY env var)
            host: Langfuse host URL (defaults to LANGFUSE_HOST env var)
        """
        self.mapper = LangfuseSessionMapper(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )

    def fetch_session(self, session_id: str) -> Session:
        """Fetch a session from Langfuse by session_id.

        Args:
            session_id: The session.id used when creating the agent traces

        Returns:
            Session object with all traces
        """
        return self.mapper.get_session(session_id)

    def fetch_session_by_trace_id(self, trace_id: str) -> Session:
        """Fetch a session from Langfuse by trace_id.

        Args:
            trace_id: The Langfuse trace ID

        Returns:
            Session object containing the single trace
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


def evaluate_langfuse_session(
    session_id: str,
    evaluators: list[Callable[[Session], dict[str, Any]]],
    public_key: str | None = None,
    secret_key: str | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    """Convenience function to fetch and evaluate a Langfuse session.

    Args:
        session_id: The session.id to fetch from Langfuse
        evaluators: List of evaluator functions
        public_key: Langfuse public key (optional)
        secret_key: Langfuse secret key (optional)
        host: Langfuse host URL (optional)

    Returns:
        Dictionary with session info and evaluator results
    """
    evaluator = PostHocEvaluator(
        public_key=public_key,
        secret_key=secret_key,
        host=host,
    )

    session = evaluator.fetch_session(session_id)
    results = evaluator.run_evaluators(session, evaluators)

    return {
        "session_id": session_id,
        "trace_count": len(session.traces),
        "evaluator_results": results,
    }


# Example usage
if __name__ == "__main__":
    import os

    # Ensure environment variables are set
    required_vars = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        print(f"Missing environment variables: {missing}")
        print("Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY to use this module.")
        exit(1)

    # Example: fetch a session and print basic info
    test_session_id = os.environ.get("TEST_SESSION_ID", "test_session")
    print(f"Fetching session: {test_session_id}")

    evaluator = PostHocEvaluator()
    session = evaluator.fetch_session(test_session_id)

    print(f"Found {len(session.traces)} traces")
    for trace in session.traces:
        print(f"  Trace {trace.trace_id}: {len(trace.spans)} spans")
