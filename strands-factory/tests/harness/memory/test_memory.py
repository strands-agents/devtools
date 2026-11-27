"""Session ids and the conversation view."""

from __future__ import annotations


def test_runtime_session_id_pads_short_and_keeps_long():
    from strandly.memory.session import runtime_session_id

    short = runtime_session_id("s-1")
    assert len(short) >= 33 and "/" not in short
    # Deterministic: same input → same affinity key (so a later poll lands on the same instance).
    assert runtime_session_id("s-1") == short
    # Slashes are sanitized (Memory/Runtime ids are slash-free).
    assert "/" not in runtime_session_id("gh/owner/repo/issue/1")
    # An already-long id is returned unchanged (still slash-free).
    long_id = "a" * 40
    assert runtime_session_id(long_id) == long_id


def _turn(role, text, *, tool_use=False):
    return (role, text, tool_use)


def test_final_assistant_text():
    from strandly.memory.session import final_assistant_text

    assert final_assistant_text([("user", "x"), ("assistant", "A"), ("assistant", "B")]) == "B"
    assert final_assistant_text([("user", "x")]) is None
    # Empty assistant text is skipped (a pure tool-use turn carries no answer text).
    assert final_assistant_text([("assistant", "real"), ("assistant", "   ")]) == "real"
    # The 3-tuple (role, text, has_tool_use) conversation form works too.
    assert final_assistant_text([_turn("assistant", "x", tool_use=True)]) == "x"


def test_conversation_settled_is_tool_use_aware():
    from strandly.memory.session import conversation_settled

    # Nothing yet → not settled.
    assert conversation_settled([]) is False
    # Only the user ask so far → not settled.
    assert conversation_settled([_turn("user", "review PR")]) is False
    # A narration ("Let me clone…") emitted *before* a tool call carries a toolUse block. The old
    # "any assistant after the last user" rule called this settled and returned the narration as
    # the final result. The tool_use-aware rule keeps it running.
    assert (
        conversation_settled(
            [_turn("user", "review PR"), _turn("assistant", "Let me clone…", tool_use=True)]
        )
        is False
    )
    # A tool result (non-assistant message) as the latest message → still running.
    assert (
        conversation_settled(
            [
                _turn("user", "review PR"),
                _turn("assistant", "cloning", tool_use=True),
                _turn("user", "<tool result>"),
            ]
        )
        is False
    )
    # Only when a terminal, text-only assistant answer is the last message → settled.
    assert (
        conversation_settled(
            [
                _turn("user", "review PR"),
                _turn("assistant", "cloning", tool_use=True),
                _turn("user", "<tool result>"),
                _turn("assistant", "Here is my review: LGTM."),
            ]
        )
        is True
    )


def test_the_legacy_agentcore_session_manager_is_gone():
    # strands-agents >=1.63 refuses a RepositorySessionManager (AgentCoreMemorySessionManager,
    # FileSessionManager) alongside a context manager — every turn would fail at Agent() build.
    # Nothing in the session layer may construct one again.
    import inspect

    import strandly.memory.session as mem

    src = inspect.getsource(mem)
    assert "AgentCoreMemorySessionManager(" not in src
    assert "FileSessionManager(" not in src
