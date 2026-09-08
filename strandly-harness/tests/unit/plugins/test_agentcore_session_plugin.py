"""AgentCoreSessionPlugin: restore/record, and the adopt-vs-warm-up ordering.

The critical invariant: when a prior session id is persisted, ``restore`` must ADOPT it and must NOT
warm up (which would start a fresh session and make adoption a no-op — defeating session reuse and
losing the prior session's filesystem). When nothing is adopted, it warms up so the git bootstrap
overlaps the agent's first non-sandbox work.
"""

from __future__ import annotations

from strandly_harness.plugins.agentcore_session import SESSION_STATE_KEY, AgentCoreSessionPlugin


class FakeSandbox:
    """A stand-in that records adopt/warm_up calls and satisfies the plugin's isinstance check."""

    def __init__(self, *, session_id=None, adopt_result=True):
        self._session_id = session_id
        self._adopt_result = adopt_result
        self.owns_session = True
        self.adopt_calls: list[str] = []
        self.warm_up_calls = 0

    @property
    def session_id(self):
        return self._session_id

    def adopt_session(self, sid):
        self.adopt_calls.append(sid)
        return self._adopt_result

    def warm_up(self):
        self.warm_up_calls += 1


class FakeState:
    def __init__(self, initial=None):
        self._d = dict(initial or {})

    def get(self, k):
        return self._d.get(k)

    def set(self, k, v):
        self._d[k] = v

    def delete(self, k):
        self._d.pop(k, None)


class FakeAgent:
    def __init__(self, sandbox, state):
        self.sandbox = sandbox
        self.state = state


class FakeEvent:
    def __init__(self, agent):
        self.agent = agent


def _patch_isinstance(monkeypatch):
    # The plugin's _owned_agentcore_sandbox does an isinstance(sandbox, AgentCoreSandbox) check.
    # Point that name at FakeSandbox so our fake qualifies without a real bedrock-agentcore client.
    import strandly_harness.plugins.agentcore_session as mod

    monkeypatch.setattr(mod, "_owned_agentcore_sandbox", lambda agent: getattr(agent, "sandbox", None))


def test_restore_adopts_and_does_not_warm_up(monkeypatch):
    # A persisted id → adopt it, and DO NOT warm up (warming would defeat adoption).
    _patch_isinstance(monkeypatch)
    sb = FakeSandbox(adopt_result=True)
    agent = FakeAgent(sb, FakeState({SESSION_STATE_KEY: "prior-session-id-000000000000000000"}))
    AgentCoreSessionPlugin().restore(FakeEvent(agent))
    assert sb.adopt_calls == ["prior-session-id-000000000000000000"]
    assert sb.warm_up_calls == 0  # <-- the regression guard


def test_restore_warms_up_when_no_prior_session(monkeypatch):
    # No persisted id → warm up so the git bootstrap overlaps the agent's first work.
    _patch_isinstance(monkeypatch)
    sb = FakeSandbox()
    agent = FakeAgent(sb, FakeState({}))
    AgentCoreSessionPlugin().restore(FakeEvent(agent))
    assert sb.adopt_calls == []
    assert sb.warm_up_calls == 1


def test_restore_warms_up_when_adoption_fails(monkeypatch):
    # A persisted id that no longer adopts (adopt returns False) → fall back to warming a fresh one.
    _patch_isinstance(monkeypatch)
    sb = FakeSandbox(adopt_result=False)
    agent = FakeAgent(sb, FakeState({SESSION_STATE_KEY: "expired-session-id-0000000000000000"}))
    AgentCoreSessionPlugin().restore(FakeEvent(agent))
    assert sb.adopt_calls == ["expired-session-id-0000000000000000"]
    assert sb.warm_up_calls == 1


def test_record_persists_live_session_id(monkeypatch):
    _patch_isinstance(monkeypatch)
    sb = FakeSandbox(session_id="live-session-id-00000000000000000000")
    state = FakeState({})
    AgentCoreSessionPlugin()._record(FakeAgent(sb, state))
    assert state.get(SESSION_STATE_KEY) == "live-session-id-00000000000000000000"


def test_record_clears_stale_id_when_session_gone(monkeypatch):
    _patch_isinstance(monkeypatch)
    sb = FakeSandbox(session_id=None)  # closed / never started
    state = FakeState({SESSION_STATE_KEY: "old"})
    AgentCoreSessionPlugin()._record(FakeAgent(sb, state))
    assert state.get(SESSION_STATE_KEY) is None
