"""The ``set_session_title`` tool: the agent names the conversation it is in.

The title is one run-ledger row per session (``strandly.core.session_ids.session_title_key``),
which the dashboard reads for its session list and lets people rename. The tool takes no session
argument: it writes only the session the agent was built for, and only while the current turn
runs in that same session. Built at depth 0, unscoped, with a run ledger and a session id.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from strands import ToolContext, tool

from strandly.core.config import Config
from strandly.core.context import RUN_CONTEXT_STATE_KEY, RuntimeContext
from strandly.core.session_ids import (
    SESSION_TITLE_MAX_LEN,
    clean_session_title,
    sanitize_session_id,
    session_title_key,
)

logger = logging.getLogger(__name__)

SESSION_TITLE_TOOL_NAME = "set_session_title"


# The dashboard stamps person-made titles ``updated_by = "portal[:<subject>]"``.
PERSON_UPDATED_BY_PREFIX = "portal"


def _is_conditional_check_failure(error: Exception) -> bool:
    response = getattr(error, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code == "ConditionalCheckFailedException"


def _result(status: str, text: str) -> dict[str, Any]:
    return {"status": status, "content": [{"text": text}]}


def ledger_table_factory(config: Config) -> Callable[[], Any]:
    """A lazy, cached boto3 ``Table`` for the run ledger (no AWS work until the first call)."""
    cache: dict[str, Any] = {}

    def table() -> Any:
        if "table" not in cache:
            import boto3

            session = boto3.Session(region_name=config.aws_region) if config.aws_region else boto3.Session()
            cache["table"] = session.resource("dynamodb").Table(config.run_ledger_table or "")
        return cache["table"]

    return table


def make_session_title_tool(ctx: RuntimeContext, table: Callable[[], Any]) -> Any:
    """Return the tool bound to ``ctx.session_id``; ``table`` yields the run-ledger ``Table``."""
    bound = sanitize_session_id(ctx.session_id or "")

    @tool(name=SESSION_TITLE_TOOL_NAME, context=True)
    def set_session_title(
        title: str, tool_context: ToolContext, replace_user_title: bool = False,
    ) -> dict[str, Any]:
        """Set or replace the title of this conversation, shown in the session list.

        A short plain-text line (at most 80 characters) naming the topic; an empty title clears it.
        It applies only to the current conversation. A title a person set is kept unless
        ``replace_user_title`` is true; pass that only when the user asks you to rename it.

        Args:
            title: The new title, e.g. "Flaky retry test in strands-agents/sdk-python".
            replace_user_title: Overwrite a title a person set in the portal.
        """
        current = (tool_context.invocation_state or {}).get(RUN_CONTEXT_STATE_KEY)
        current_id = current.session_id if isinstance(current, RuntimeContext) else None
        # Authority is the current turn's session; a cached agent must never write another one.
        if not ctx.session_id or not current_id or sanitize_session_id(current_id) != bound:
            return _result("error", "This run has no conversation session to title.")
        try:
            text, note = clean_session_title(title), ""
        except TypeError as exc:
            return _result("error", str(exc))
        except ValueError:
            text = clean_session_title(title, truncate=True)
            note = f" (shortened to {SESSION_TITLE_MAX_LEN} characters)"
        item: dict[str, Any] = {
            "task_id": session_title_key(bound),
            "session_id": bound,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": "agent",
        }
        if text:
            item["title"] = text
        # The run's bound person, as the dashboard records it on a rename.
        person = current.person if isinstance(current, RuntimeContext) else None
        if person is not None:
            item["person"] = f"github:{person.github}" if person.github else f"person:{person.person_id}"
        kwargs: dict[str, Any] = {} if replace_user_title else {
            "ConditionExpression": "attribute_not_exists(#title) OR NOT begins_with(#by, :person)",
            "ExpressionAttributeNames": {"#title": "title", "#by": "updated_by"},
            "ExpressionAttributeValues": {":person": PERSON_UPDATED_BY_PREFIX},
        }
        try:
            table().put_item(Item=item, **kwargs)
        except Exception as exc:  # noqa: BLE001 - reported to the model; a title must not fail the turn
            if _is_conditional_check_failure(exc):
                return _result("error", "A person titled this conversation; it was left unchanged.")
            logger.warning("session title write failed (session_id=%s)", bound, exc_info=True)
            return _result("error", "Could not save the title right now; carry on without it.")
        logger.info("session title %s by agent (session_id=%s)", "set" if text else "cleared", bound)
        if not text:
            return _result("success", "Title cleared.")
        return _result("success", f"Title set: {text}{note}")

    return set_session_title
