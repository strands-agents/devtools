"""Long-term memory on AgentCore Memory records (``memory/long_term.py``).

The stores are the real ``AgentCoreMemoryStore`` inside the real ``strands.memory.MemoryManager``;
the data plane is ``fake_agentcore.FakeAgentCoreClient`` (every request validated against the
installed botocore model, so an over-long search query fails exactly as the service would).
Records are seeded straight into the fake, the way AgentCore extraction would leave them.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import strandly.memory.long_term as long_term
from fake_agentcore import MEMORY_ID, THROTTLED, FakeAgentCoreClient
from model_seam import use_model
from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.memory.long_term import RETRIEVAL_QUERY_MAX_CHARS, build_memory_manager
from strandly.plugins.memory_recall import MemoryRecall

CFG = Config(values={"AGENTCORE_MEMORY_ID": MEMORY_ID, "AWS_REGION": "us-west-2"})
DM_EVENT = {"slackChannelId": "D1", "slackUserId": "U0ABC", "triggerSource": "slack:U0ABC"}
TEAM_NAMESPACES = ["/facts/team/", "/preferences/team/"]
PERSONAL_NAMESPACES = ["/facts/slack-user-U0ABC/", "/preferences/slack-user-U0ABC/"]


def _dm(user: str = "U0ABC") -> RuntimeContext:
    return RuntimeContext(session_id=f"slack-T1-D{user}-1.2", event={"slackChannelId": "D1", "slackUserId": user, "triggerSource": f"slack:{user}"})


TEAM = RuntimeContext(session_id="gh-o-r-issue-1")


@pytest.fixture
def plane(monkeypatch) -> FakeAgentCoreClient:
    fake = FakeAgentCoreClient()
    monkeypatch.setattr(long_term, "_memory_client", lambda _p, _r: SimpleNamespace(gmdp_client=fake))
    return fake


def _seed(plane: FakeAgentCoreClient, namespace: str, text: str) -> None:
    now = datetime.now(timezone.utc)
    plane.records.append(
        {
            "memoryRecordId": f"mem-{len(plane.records):040d}",
            "content": {"text": text},
            "memoryStrategyId": "facts-0123456789",
            "namespaces": [namespace],
            "createdAt": now,
            "timestamp": now,
        }
    )


def _recall(ctx: RuntimeContext, query: str) -> list[str]:
    manager = build_memory_manager(CFG, ctx)
    return [e.content for e in asyncio.run(manager.search(query))]


def _searched(plane: FakeAgentCoreClient) -> list[str]:
    return sorted(p["namespace"] for op, p in plane.calls if op == "RetrieveMemoryRecords")


# --- gating and stores -----------------------------------------------------------------------------


def test_long_term_memory_is_keyed_on_the_memory_id(plane):
    assert Config(values={}).use_long_term_memory is False
    assert build_memory_manager(Config(values={}), TEAM) is None
    assert CFG.use_long_term_memory is True
    assert build_memory_manager(CFG, TEAM) is not None


def test_a_team_run_reads_the_team_stores_only(plane):
    manager = build_memory_manager(CFG, TEAM)
    assert [s.name for s in manager._stores] == ["strandly-memory", "team-preferences"]
    _recall(TEAM, "anything")
    assert _searched(plane) == TEAM_NAMESPACES


def test_a_dm_reads_the_persons_stores_and_the_teams(plane):
    manager = build_memory_manager(CFG, _dm())
    assert [s.name for s in manager._stores] == [
        "strandly-memory", "team-preferences", "personal", "personal-preferences",
    ]
    _recall(_dm(), "anything")
    assert _searched(plane) == sorted(TEAM_NAMESPACES + PERSONAL_NAMESPACES)


# The fields the dashboard API sends for the person bound to the verified Cognito sub.
_PORTAL_PERSON = {"triggerSource": "portal:chat", "portalUser": "11111111-1111-4111-8111-111111111111", "personGithub": "octocat", "personId": "octo"}


def test_a_portal_run_of_a_person_with_slack_shares_their_dm_memory(plane):
    ctx = RuntimeContext(session_id="portal-1", event={**_PORTAL_PERSON, "personSlackUserId": "U0ABC"})
    _seed(plane, "/facts/slack-user-U0ABC/", "dm fact")
    manager = build_memory_manager(CFG, ctx)
    assert [s.name for s in manager._stores][2:] == ["personal", "personal-preferences"]
    assert "THIS conversation (U0ABC)" in manager._stores[2].description
    assert _recall(ctx, "fact") == ["dm fact"]
    assert _searched(plane) == sorted(TEAM_NAMESPACES + PERSONAL_NAMESPACES)


def test_a_portal_run_of_a_person_without_slack_reads_their_own_scope(plane):
    ctx = RuntimeContext(session_id="cli-1", event=_PORTAL_PERSON)
    _recall(ctx, "anything")
    assert _searched(plane) == sorted(TEAM_NAMESPACES + ["/facts/person-octo/", "/preferences/person-octo/"])
    personal = build_memory_manager(CFG, ctx)._stores[2]
    assert "THIS conversation (person-octo)" in personal.description


def test_a_portal_run_with_an_unknown_login_is_team_only(plane):
    _recall(RuntimeContext(session_id="cli-1", event={"triggerSource": "portal:chat", "personGithub": "x"}), "q")
    assert _searched(plane) == TEAM_NAMESPACES


def test_person_fields_without_the_verified_portal_user_are_team_only(plane):
    """Only the dashboard API's verified resolution (person fields next to ``portalUser``) counts."""
    forged = {k: v for k, v in _PORTAL_PERSON.items() if k != "portalUser"}
    _recall(RuntimeContext(session_id="cli-1", event={**forged, "personSlackUserId": "U0ABC"}), "q")
    assert _searched(plane) == TEAM_NAMESPACES


@pytest.mark.parametrize(
    "event",
    [
        {"slackChannelId": "C1", "slackUserId": "U0ABC"},  # shared channel
        {"slackChannelId": "G1", "slackUserId": "U0ABC"},  # group DM
        {"slackChannelId": "D1"},  # DM without a verified user id
        {"slackChannelId": "D1", "slackUserId": "U0ABC"},  # no Slack ingress trigger
        {"slackChannelId": "D1", "slackUserId": "U0ABC", "triggerSource": "portal:chat"},
    ],
)
def test_only_a_verified_dm_gets_a_personal_store(plane, event):
    manager = build_memory_manager(CFG, RuntimeContext(session_id="slack-T1-C1-1.2", event=event))
    assert [s.name for s in manager._stores] == ["strandly-memory", "team-preferences"]


def test_a_person_recalls_their_own_records_never_anothers(plane):
    _seed(plane, "/facts/team/", "deploys need two approvals")
    _seed(plane, "/facts/slack-user-U0ABC/", "U0ABC owns the deploys dashboard")
    _seed(plane, "/facts/slack-user-U0XYZ/", "U0XYZ owns the deploys pager")
    assert sorted(_recall(_dm("U0ABC"), "who owns deploys")) == [
        "U0ABC owns the deploys dashboard", "deploys need two approvals",
    ]
    assert _recall(TEAM, "who owns deploys") == ["deploys need two approvals"]


def test_every_store_is_read_only_and_there_is_no_add_tool(plane, text_model):
    from strands import Agent

    for ctx in (TEAM, _dm()):
        manager = build_memory_manager(CFG, ctx)
        assert all(s.writable is False for s in manager._stores)
        assert [t.tool_name for t in manager.tools] == ["search_memory"]
        # A whole turn (recall injection included) reads but never writes, and an explicit add
        # finds no store to write to.
        Agent(model=text_model("ok"), plugins=[manager, MemoryRecall(manager)], callback_handler=None)(
            "remember: uv builds"
        )
        with pytest.raises(ValueError):
            asyncio.run(manager.add("uv builds"))
    ops = {op for op, _ in plane.calls}
    assert "RetrieveMemoryRecords" in ops
    assert not ops & {"BatchCreateMemoryRecords", "BatchUpdateMemoryRecords", "CreateEvent"}


def test_recall_renders_the_sdk_memory_block():
    from strands.memory import MemoryEntry, MemoryManager

    from strandly.plugins.memory_recall import render_memories

    entries = [
        MemoryEntry(content="a < b & \"c\" 'd'", store_name="team's \"store\""),
        MemoryEntry(content="no store"),
    ]
    # The SDK's own renderer for its per-call injection reads nothing from the manager.
    assert render_memories(entries) == MemoryManager._default_injection_format(None, entries)  # type: ignore[arg-type]


def test_recall_is_one_injected_block_on_the_ask(plane, text_model):
    from strands import Agent

    _seed(plane, "/facts/team/", "the build uses uv")
    manager = build_memory_manager(CFG, TEAM)
    agent = Agent(model=text_model("ok"), plugins=[manager, MemoryRecall(manager)], callback_handler=None)
    agent("what does the build use")
    blocks = [b["injected"] for b in agent.messages[0]["content"] if "injected" in b]
    assert [b["source"] for b in blocks] == ["memory-recall:injector"]
    assert blocks[0]["text"] == '<memory>\n<entry source="strandly-memory">the build uses uv</entry>\n</memory>'


# --- the search query cap --------------------------------------------------------------------------


def test_an_over_long_query_keeps_its_tail_and_still_recalls(plane, caplog):
    _seed(plane, "/facts/team/", "deploys need two approvals")
    query = "x " * 20_000 + "how many approvals do deploys need"
    assert _recall(TEAM, query) == ["deploys need two approvals"]
    sent = [p["searchCriteria"]["searchQuery"] for op, p in plane.calls if op == "RetrieveMemoryRecords"]
    assert sent and all(len(q) == RETRIEVAL_QUERY_MAX_CHARS and q.endswith("deploys need") for q in sent)
    assert "search query too long" in caplog.text


def test_the_cap_covers_the_per_turn_recall_injection(plane, text_model):
    from strands import Agent

    _seed(plane, "/facts/team/", "the build uses uv")
    manager = build_memory_manager(CFG, TEAM)
    agent = Agent(model=text_model("ok"), plugins=[manager, MemoryRecall(manager)], callback_handler=None)
    agent("y " * 8_000 + "what does the build use")
    queries = [p["searchCriteria"]["searchQuery"] for op, p in plane.calls if op == "RetrieveMemoryRecords"]
    assert queries and all(len(q) <= RETRIEVAL_QUERY_MAX_CHARS for q in queries)


def test_a_short_query_passes_through_unchanged(plane):
    _recall(TEAM, "exact query text")
    assert {p["searchCriteria"]["searchQuery"] for op, p in plane.calls if op == "RetrieveMemoryRecords"} == {
        "exact query text"
    }


# --- failures fail open ----------------------------------------------------------------------------


def test_a_failed_store_search_leaves_the_others(plane, caplog):
    _seed(plane, "/facts/team/", "deploys need two approvals")
    _seed(plane, "/facts/slack-user-U0ABC/", "U0ABC reviews deploys on Fridays")
    plane.fail("RetrieveMemoryRecords", THROTTLED, when=lambda p: p.get("namespace") == "/facts/team/")
    assert _recall(_dm(), "deploys") == ["U0ABC reviews deploys on Fridays"]
    assert "store search failed" in caplog.text


def test_a_turn_still_answers_when_every_search_fails(plane, text_model):
    from strands import Agent

    plane.fail("RetrieveMemoryRecords", THROTTLED)
    manager = build_memory_manager(CFG, TEAM)
    agent = Agent(model=text_model("ok"), plugins=[manager, MemoryRecall(manager)], callback_handler=None)
    assert str(agent("what does the build use")).strip() == "ok"
    assert any(op == "RetrieveMemoryRecords" for op, _ in plane.calls)


# --- the agent wiring ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_agent_gets_search_and_never_an_add_tool(plane, fake_model, tmp_path):
    from strandly.core.agent import build_agent

    team = await build_agent(CFG, RuntimeContext(cwd=str(tmp_path)), model=fake_model)
    dm = await build_agent(CFG, RuntimeContext(cwd=str(tmp_path), event=DM_EVENT), model=fake_model)
    off = await build_agent(Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model)
    for agent in (team, dm):
        names = set(agent.tool_names)
        assert "search_memory" in names
        assert not {n for n in names if n.startswith("add_") and "memory" in n}
    assert "search_memory" not in off.tool_names


def _subagent_child(monkeypatch, fake_model, tmp_path, event):
    """A child built by the ``subagent`` tool's builder for a parent run on ``event``."""
    from strands_harness.tools.subagent import AgentSpec

    import strandly.core.agent as agent_mod
    from strandly.tools.subagent import make_builder

    built = []

    def _capture(config, ctx):
        built.append(build_memory_manager(config, ctx))
        return built[-1]

    monkeypatch.setattr(agent_mod, "build_memory_manager", _capture)
    use_model(monkeypatch, lambda _model: fake_model)
    parent_ctx = RuntimeContext(cwd=str(tmp_path), session_id="s1", event=event)
    child = make_builder(CFG, parent_ctx, None)(AgentSpec(task="x"))
    return child, built[0]


def test_a_subagent_child_reads_the_same_stores(plane, fake_model, tmp_path, monkeypatch):
    child, memory = _subagent_child(monkeypatch, fake_model, tmp_path, DM_EVENT)
    names = set(child.tool_names)
    assert "search_memory" in names
    assert not {n for n in names if n.startswith("add_") and "memory" in n}
    assert [s.name for s in memory._stores] == [
        "strandly-memory", "team-preferences", "personal", "personal-preferences",
    ]


def test_a_subagent_child_outside_a_dm_reads_the_team_stores_only(
    plane, fake_model, tmp_path, monkeypatch
):
    channel = {"slackChannelId": "C1", "slackUserId": "U0ABC"}
    child, memory = _subagent_child(monkeypatch, fake_model, tmp_path, channel)
    assert "search_memory" in child.tool_names
    assert [s.name for s in memory._stores] == ["strandly-memory", "team-preferences"]


def test_the_agent_cache_key_carries_the_personal_scope():
    from strandly.serve.cache import agent_cache_key

    dm = RuntimeContext(session_id="slack-T-D1-1", event=DM_EVENT)
    assert agent_cache_key(dm) == "slack-T-D1-1#slack-user-U0ABC"
    assert agent_cache_key(RuntimeContext(session_id="gh-o-r-1")) == "gh-o-r-1"
