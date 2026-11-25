"""AgentCore Runtime V2 snapshot safety: nothing per-instance is resolved at import.

V2 snapshots the process after startup and clones it into every session, so the deployed
entrypoint must not read the config secret, build AWS clients, or seed ``random`` at import — those
happen on the first request, after the restore (``serve/instance_init.py``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import random
import threading
from pathlib import Path

import pytest

from strandly.core.config import Config
from strandly.serve.instance_init import InstanceInit, reseed_process_randomness

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_loader_is_not_called_until_first_use():
    calls: list[str] = []
    wired: list[Config] = []

    def load() -> Config:
        calls.append("load")
        return Config(values={})

    init = InstanceInit(load, wired.append)
    assert calls == [] and wired == [] and init.ready is False

    asyncio.run(init.ensure_ready())
    asyncio.run(init.ensure_ready())
    assert calls == ["load"]
    assert len(wired) == 1 and init.ready is True


def test_loaded_value_is_wired_immediately():
    wired: list[Config] = []
    cfg = Config(values={})
    init = InstanceInit(cfg, wired.append)
    assert wired == [cfg] and init.ready is True


def test_concurrent_first_requests_load_once():
    calls: list[int] = []
    gate = threading.Event()

    def load() -> Config:
        gate.wait(1)
        calls.append(1)
        return Config(values={})

    init = InstanceInit(load, lambda _c: None)
    threads = [threading.Thread(target=init.ensure_ready_sync) for _ in range(8)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join()
    assert calls == [1]


def test_failed_load_is_not_cached():
    attempts: list[int] = []

    def load() -> Config:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("secrets manager unavailable")
        return Config(values={})

    init = InstanceInit(load, lambda _c: None)
    with pytest.raises(RuntimeError):
        init.ensure_ready_sync()
    assert init.ready is False
    init.ensure_ready_sync()
    assert init.ready is True and len(attempts) == 2


def test_reseed_breaks_a_cloned_random_state():
    # A snapshot clone starts from the parent's generator state; reseeding must diverge from it.
    random.seed(1234)
    snapshot_state = random.getstate()
    clone = random.Random()
    clone.setstate(snapshot_state)
    expected_if_cloned = [clone.random() for _ in range(4)]

    reseed_process_randomness()
    assert [random.random() for _ in range(4)] != expected_if_cloned


def test_instance_init_reseeds_on_first_use(monkeypatch):
    seeds: list[object] = []
    monkeypatch.setattr(random, "seed", lambda *a, **k: seeds.append(a))
    init = InstanceInit(lambda: Config(values={}), lambda _c: None)
    assert seeds == []
    init.ensure_ready_sync()
    assert seeds == [()]


def test_build_app_with_loader_defers_config_and_clients(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    from strandly.ops.ledger import RunLedger
    from strandly.serve import agentcore_app as ac

    events: list[str] = []

    def load() -> Config:
        events.append("load")
        return Config(values={})

    real_from_config = RunLedger.from_config

    def spy_from_config(config):
        events.append("ledger")
        return real_from_config(config)

    monkeypatch.setattr(RunLedger, "from_config", staticmethod(spy_from_config))

    app = ac.build_app(load)
    assert events == [], "build_app(loader) must not load config or build clients at import"

    invoke = app.handlers["main"]
    out = asyncio.run(invoke({"action": "poll", "taskId": "nope"}))
    assert isinstance(out, dict)
    assert events == ["load", "ledger"]

    asyncio.run(invoke({"action": "poll", "taskId": "nope"}))
    assert events == ["load", "ledger"], "config is loaded once per instance"


def test_import_main_does_no_secret_read_or_aws_client(monkeypatch):
    """``import main`` (what the runtime does before V2 snapshots) must not touch AWS."""
    pytest.importorskip("bedrock_agentcore")
    import boto3

    from strandly.core import config as config_mod

    def boom(*_a, **_k):
        raise AssertionError("network/credential work at import time")

    monkeypatch.setenv(config_mod.SECRETS_ARN, "test-config-secret")
    monkeypatch.setattr(config_mod, "_load_secret", boom)
    monkeypatch.setattr(boto3, "client", boom)
    monkeypatch.setattr(boto3.Session, "client", boom)

    spec = importlib.util.spec_from_file_location("strandly_runtime_main", REPO_ROOT / "harness" / "main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.app is not None


def _clone_first_request(monkeypatch) -> tuple[int, float]:
    """One V2 clone: start from the snapshot's ``random`` state, serve one real HTTP request.

    The app is built exactly as in production under ``opentelemetry-instrument``: the Starlette
    instrumentation adds its server-span middleware when the app is CONSTRUCTED (mirrored here by
    instrumenting in ``__init__``), before ``build_app`` adds its own. Returns the request's server
    span id and the first ``random.random()`` the config loader sees.
    """
    pytest.importorskip("bedrock_agentcore")
    from bedrock_agentcore import runtime as bac_runtime
    from opentelemetry.instrumentation.starlette import StarletteInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import SpanKind
    from starlette.testclient import TestClient

    from strandly.serve import agentcore_app as ac

    exporter = InMemorySpanExporter()
    provider = TracerProvider()  # RandomIdGenerator: span ids come from random.getrandbits
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    base = bac_runtime.BedrockAgentCoreApp

    class InstrumentedApp(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            StarletteInstrumentor.instrument_app(self, tracer_provider=provider)

    seen: list[float] = []

    def load() -> Config:
        seen.append(random.random())  # what botocore's retry jitter would draw
        return Config(values={})

    with monkeypatch.context() as patch:
        patch.setattr(bac_runtime, "BedrockAgentCoreApp", InstrumentedApp)
        app = ac.build_app(load)
    random.seed(1234)  # the state every clone inherits from the snapshot
    with TestClient(app) as client:
        client.post("/invocations", json={"action": "poll", "taskId": "nope"})
    (server,) = [s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER]
    return server.context.span_id, seen[0]


def test_reseed_runs_before_the_first_span_and_the_config_load(monkeypatch):
    # Review R3 S8: two clones of one snapshot must not share their first span id or the jitter the
    # config read draws. Fails if the reseed moves back inside the entrypoint (after the server span
    # and after Config.load) or if another middleware is added outside ReseedRandomnessMiddleware.
    first = _clone_first_request(monkeypatch)
    second = _clone_first_request(monkeypatch)
    assert first[0] != second[0], "cloned instances opened their first span with the same id"
    assert first[1] != second[1], "cloned instances drew the same jitter during the config load"
    random.seed(1234)
    assert first[1] != random.random()
