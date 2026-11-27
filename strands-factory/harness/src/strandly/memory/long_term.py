"""Long-term recall: AgentCore Memory records read through a ``MemoryManager``.

Enabled by ``AGENTCORE_MEMORY_ID``. Records live in per-actor namespaces (``memory/scopes.py``) and
are written only by the Memory's extraction strategies; the agent has no write tool. Reads go
through read-only stores, one per namespace in scope. The actor comes from the ingress payload,
never from the model, and ``serve.cache.agent_cache_key`` folds it into the cache key.
"""

from __future__ import annotations

import functools
import logging
from functools import lru_cache
from typing import Any

from strandly.core.config import Config
from strandly.core.constants import (
    MEMORY_FACTS_NAMESPACE_TEMPLATE,
    MEMORY_PREFERENCES_NAMESPACE_TEMPLATE,
)
from strandly.core.context import RuntimeContext
from strandly.memory.scopes import (
    PERSONAL_ACTOR_PREFIX,
    TEAM_ACTOR,
    memory_session_id,
    personal_actor,
)

logger = logging.getLogger(__name__)

# A store *name* is the handle the model sees (in the search_memory description and in every
# result's ``store_name``).
TEAM_STORE_NAME = "strandly-memory"
TEAM_PREFERENCES_STORE_NAME = "team-preferences"
PERSONAL_STORE_NAME = "personal"
PERSONAL_PREFERENCES_STORE_NAME = "personal-preferences"

TEAM_STORE_DESCRIPTION = (
    "team-wide memory shared by everyone — code facts, repo conventions, procedures, decisions, "
    "past mistakes. Use for anything about the codebase or the team."
)
TEAM_PREFERENCES_STORE_DESCRIPTION = "how the team likes work done, learned from team conversations."

# ``RetrieveMemoryRecords`` caps ``searchCriteria.searchQuery`` at 10,000 characters.
RETRIEVAL_QUERY_MAX_CHARS = 10_000


def personal_store_description(user_id: str, where: str = "Slack conversation") -> str:
    return (
        f"private memory of the one person in THIS {where} ({user_id}) — their "
        "context, what they own, what they asked for. No other user's memory is reachable from here."
    )


def personal_preferences_store_description(user_id: str, where: str = "Slack conversation") -> str:
    return (
        f"working preferences of the person in THIS {where} ({user_id}), learned from "
        "their conversations."
    )


@lru_cache(maxsize=8)
def _memory_client(profile: str | None, region: str | None) -> Any:
    """One ``MemoryClient`` per process (its data-plane client is what the stores call)."""
    from bedrock_agentcore.memory import MemoryClient

    from strandly.core.config import _session

    return MemoryClient(
        region_name=region, boto3_session=_session(profile, region), integration_source="strands"
    )


def memory_client(config: Config) -> Any:
    return _memory_client(config.get("AWS_PROFILE"), config.aws_region)


def build_memory_manager(config: Config, ctx: RuntimeContext | None = None) -> Any | None:
    """A read-only ``MemoryManager`` over the run's namespaces, or ``None`` without a Memory.

    Team facts and preferences always; a Slack DM adds the person's facts and preferences. Its own
    per-call injection is off: ``core.agent`` pairs it with the ``MemoryRecall`` plugin.
    """
    if not config.use_long_term_memory:
        return None

    from bedrock_agentcore.memory.integrations.strands.memorystore import AgentCoreMemoryStore
    from strands.memory import MemoryManager

    client = memory_client(config)
    memory_id = str(config.memory_id)
    raw_session = (ctx.session_id or ctx.session_key) if ctx is not None else None
    session_id = memory_session_id(raw_session) if raw_session else "recall"

    def store(actor: str, namespace: str, name: str, description: str) -> Any:
        return _cap_search_query(
            AgentCoreMemoryStore(
                memory_id=memory_id,
                actor_id=actor,
                session_id=session_id,
                namespace=namespace,
                name=name,
                description=description,
                writable=False,
                client=client,
            )
        )

    stores = [
        store(TEAM_ACTOR, MEMORY_FACTS_NAMESPACE_TEMPLATE, TEAM_STORE_NAME, TEAM_STORE_DESCRIPTION),
        store(
            TEAM_ACTOR,
            MEMORY_PREFERENCES_NAMESPACE_TEMPLATE,
            TEAM_PREFERENCES_STORE_NAME,
            TEAM_PREFERENCES_STORE_DESCRIPTION,
        ),
    ]
    actor = personal_actor(ctx)
    if actor:
        user_id = actor.removeprefix(PERSONAL_ACTOR_PREFIX)
        # A portal/CLI/ACP person shares the actor of their DMs, but this run is not a Slack chat.
        where = "Slack conversation" if ctx is not None and ctx.is_slack_dm else "conversation"
        stores.append(
            store(
                actor,
                MEMORY_FACTS_NAMESPACE_TEMPLATE,
                PERSONAL_STORE_NAME,
                personal_store_description(user_id, where),
            )
        )
        stores.append(
            store(
                actor,
                MEMORY_PREFERENCES_NAMESPACE_TEMPLATE,
                PERSONAL_PREFERENCES_STORE_NAME,
                personal_preferences_store_description(user_id, where),
            )
        )
        logger.info("long-term memory: personal scope %s enabled for this run", actor)
    # Recall reaches the model through ``plugins.memory_recall``, which keeps it in history.
    return MemoryManager(stores=stores, add_tool_config=False, injection=False)


# --- the search query cap ---------------------------------------------------------------------------


def _truncate_retrieval_query(query: Any) -> Any:
    """Keep the TAIL of an over-long search query (the newest turn), under the API's cap."""
    if not isinstance(query, str) or len(query) <= RETRIEVAL_QUERY_MAX_CHARS:
        return query
    truncated = query[-RETRIEVAL_QUERY_MAX_CHARS:]
    logger.warning(
        "long-term memory search query too long: truncated %d -> %d chars (kept the tail; "
        "RetrieveMemoryRecords caps searchQuery at %d)",
        len(query),
        len(truncated),
        RETRIEVAL_QUERY_MAX_CHARS,
    )
    return truncated


def _cap_search_query(store: Any) -> Any:
    """Wrap ``store.search`` so every caller (the tool and the per-turn injection) sends a capped query.

    The per-turn recall injection uses the whole latest user message as the query; a pasted log or
    a long issue body would otherwise fail ``RetrieveMemoryRecords`` validation and lose recall for
    the turn. Instance-local and fail-soft: a store without ``search`` is returned untouched.
    """
    original = getattr(store, "search", None)
    if not callable(original):
        return store

    @functools.wraps(original)
    async def capped_search(*args: Any, **kwargs: Any) -> Any:
        if args:
            return await original(_truncate_retrieval_query(args[0]), *args[1:], **kwargs)
        if "query" in kwargs:
            kwargs = {**kwargs, "query": _truncate_retrieval_query(kwargs["query"])}
        return await original(**kwargs)

    store.search = capped_search
    return store
