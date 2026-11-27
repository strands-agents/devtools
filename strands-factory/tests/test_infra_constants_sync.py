"""Drift guard: infra/stacks/common.py must mirror strandly.core.constants.

The CDK app (``infra/``) is a separate package with its own venv and can't import the harness
without pulling in the Strands SDK, so it hand-copies a handful of fixed provisioning values into
``infra/stacks/common.py``. That hand-mirroring is a latent drift risk — change a value in
``constants.py`` and the deployed resources silently diverge from what the code expects.

This test parses ``common.py`` with ``ast`` (no import — it pulls in ``aws_cdk``, absent from the
harness venv) and asserts every mirrored constant equals its source of truth. If you intentionally
change one, update both files and this test stays green; if you change only one, this fails.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timezone
from pathlib import Path

from strandly.core import constants
from strandly.ops import metrics

_ROOT = Path(__file__).resolve().parents[1]
_COMMON = _ROOT / "infra" / "stacks" / "common.py"
_HANDLER = _ROOT / "dashboard" / "api" / "handler.py"
_SANDBOX_MAIN = _ROOT / "sandbox" / "sandbox_main.py"

# common.py constant name -> source of truth in strandly.core.constants. (common.py uses the
# short name RUN_LEDGER_GSI; constants.py uses RUN_LEDGER_GSI_NAME — they hold the same value.)
_MIRRORED = {
    "AGENT_MAX_LIFETIME_SECONDS": constants.AGENT_MAX_LIFETIME_SECONDS,
    "MEMORY_EVENT_EXPIRY_DAYS": constants.MEMORY_EVENT_EXPIRY_DAYS,
    "MEMORY_FACTS_STRATEGY": constants.MEMORY_FACTS_STRATEGY,
    "MEMORY_FACTS_NAMESPACE_TEMPLATE": constants.MEMORY_FACTS_NAMESPACE_TEMPLATE,
    "MEMORY_PREFERENCES_STRATEGY": constants.MEMORY_PREFERENCES_STRATEGY,
    "MEMORY_PREFERENCES_NAMESPACE_TEMPLATE": constants.MEMORY_PREFERENCES_NAMESPACE_TEMPLATE,
    "RUN_LEDGER_GSI": constants.RUN_LEDGER_GSI_NAME,
    "MENTION_LOG_GSI": constants.MENTION_LOG_GSI_NAME,
    "WORK_PRODUCT_STATUS_GSI": constants.WORK_PRODUCT_STATUS_GSI_NAME,
    "WORK_PRODUCT_SESSION_GSI": constants.WORK_PRODUCT_SESSION_GSI_NAME,
    # The people directory's sparse GSIs: ops/people.py queries them by these names, the CDK
    # DataStack creates them — a drift here silently makes every lookup a miss, i.e. locks everyone
    # who isn't in the legacy secret list out.
    "PEOPLE_GITHUB_GSI": constants.PEOPLE_GITHUB_GSI_NAME,
    "PEOPLE_SLACK_GSI": constants.PEOPLE_SLACK_GSI_NAME,
    "PEOPLE_COGNITO_GSI": constants.PEOPLE_COGNITO_GSI_NAME,
    # The sandbox runtime is a CDK resource now (infra/stacks/sandbox_stack.py), so the CDK venv
    # carries its own copy of the values that configure it. SANDBOX_MOUNT_PATH is the load-bearing
    # one: the CDK sets it as the session-storage mountPath and the harness sends it as the cwd of
    # every command, so a drift means every shell command fails with "No such file or directory" —
    # in the sandbox, i.e. the one place we can't just look.
    "SANDBOX_MOUNT_PATH": constants.SANDBOX_MOUNT_PATH,
    "SANDBOX_IDLE_TIMEOUT_SECONDS": constants.SANDBOX_IDLE_TIMEOUT_SECONDS,
    "SANDBOX_MAX_LIFETIME_SECONDS": constants.SANDBOX_MAX_LIFETIME_SECONDS,
}


def _module_constants(path: Path) -> dict[str, object]:
    """Module-level ``NAME = <literal>`` assignments in a file, via ast (no import).

    Neither common.py (CDK venv) nor handler.py (standalone Lambda bundle) can import the harness,
    so we read their literals statically and compare to the canonical value in constants.py.
    """
    tree = ast.parse(path.read_text())
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    found[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    pass  # non-literal (e.g. a dataclass) — not a mirrored constant
    return found


def test_common_mirrors_constants():
    found = _module_constants(_COMMON)
    for name, expected in _MIRRORED.items():
        assert name in found, f"{name} missing from infra/stacks/common.py"
        assert found[name] == expected, (
            f"{name} drifted: common.py has {found[name]!r}, source of truth is {expected!r}. "
            "Update both infra/stacks/common.py and strandly/constants.py together."
        )


def test_dashboard_handler_gsi_name_matches():
    """The dashboard Lambda's GSI_NAME literal must equal the canonical run-ledger GSI name.

    Closes the third leg of the mirror (common.py / handler.py / constants.py): the Lambda queries
    this index by name, so a drift here silently breaks the dashboard's Runs/Overview tabs.
    """
    found = _module_constants(_HANDLER)
    assert found.get("GSI_NAME") == constants.RUN_LEDGER_GSI_NAME, (
        f"dashboard/api/handler.py GSI_NAME={found.get('GSI_NAME')!r} drifted from "
        f"constants.RUN_LEDGER_GSI_NAME={constants.RUN_LEDGER_GSI_NAME!r}."
    )


def test_dashboard_handler_work_product_session_gsi_name_matches():
    """The dashboard Lambda queries the work-product ``by_session`` GSI by the name the store uses.

    The store (``ops.work_product``) queries ``by_session`` for per-item history and the Data stack
    names that index from ``common.py`` — a drift in the handler's copy silently empties the view.
    """
    found = _module_constants(_HANDLER)
    assert found.get("WORK_PRODUCT_SESSION_GSI_NAME") == constants.WORK_PRODUCT_SESSION_GSI_NAME, (
        f"dashboard/api/handler.py WORK_PRODUCT_SESSION_GSI_NAME="
        f"{found.get('WORK_PRODUCT_SESSION_GSI_NAME')!r} drifted from "
        f"constants.WORK_PRODUCT_SESSION_GSI_NAME={constants.WORK_PRODUCT_SESSION_GSI_NAME!r}."
    )


def test_dashboard_handler_runtime_session_min_len_matches():
    """The dashboard Lambda pads chat session ids to the same floor the harness uses.

    ``handler._runtime_session_id`` mirrors ``memory.runtime_session_id`` so a chat launched from
    the dashboard lands on the same AgentCore runtime affinity / Memory key the harness would use
    for that session. A drift in the minimum length would desync the two id derivations.
    """
    found = _module_constants(_HANDLER)
    assert found.get("RUNTIME_SESSION_ID_MIN_LEN") == constants.RUNTIME_SESSION_ID_MIN_LEN, (
        f"dashboard/api/handler.py RUNTIME_SESSION_ID_MIN_LEN={found.get('RUNTIME_SESSION_ID_MIN_LEN')!r} "
        f"drifted from constants.RUNTIME_SESSION_ID_MIN_LEN={constants.RUNTIME_SESSION_ID_MIN_LEN!r}."
    )


def test_dashboard_handler_session_store_layout_matches():
    """The dashboard Lambda reads the runtime's session store by key layout it mirrors.

    A drift in any prefix / agent id / snapshot name would make ``handler.SessionStoreReader`` look
    under keys the runtime never writes, silently degrading every transcript to the ledger.
    """
    found = _module_constants(_HANDLER)
    for name in (
        "SESSION_STORE_SESSION_PREFIX",
        "SESSION_STORE_AGENT_ID",
        "SESSION_STORE_LATEST_SNAPSHOT",
        "SESSION_STORE_ACTOR_MARKER",
        "MEMORY_TEAM_ACTOR",
        "TRANSCRIPT_MAX_MESSAGES",
    ):
        assert found.get(name) == getattr(constants, name), (
            f"dashboard/api/handler.py {name}={found.get(name)!r} drifted from "
            f"constants.{name}={getattr(constants, name)!r}."
        )


def test_monitoring_stack_metric_names_match_ops_metrics():
    """The Monitoring stack alarms/widgets reference EMF metric names by hand-copied literals.

    ``infra/stacks/monitoring_stack.py`` can't import the harness (separate CDK venv), so it
    mirrors the names from ``strandly.ops.metrics``. A drift here doesn't error — the
    alarm just watches a metric that never gets data and silently never fires.
    """
    from strandly.ops import metrics

    found = _module_constants(_ROOT / "infra" / "stacks" / "monitoring_stack.py")
    mirrored = {
        "_INVOCATIONS": metrics.INVOCATIONS,
        "_COMPLETED": metrics.COMPLETED,
        "_FAILURES": metrics.FAILURES,
        "_CANCELLED": metrics.CANCELLED,
        "_DURATION_MS": metrics.DURATION_MS,
        "_LEDGER_WRITE_FAILED": metrics.LEDGER_WRITE_FAILED,
        "_TOOL_ERRORS": metrics.TOOL_ERRORS,
        "_POLL_SUCCESS": metrics.POLL_SUCCESS,
        "_DISPATCH_FAILED": metrics.DISPATCH_FAILED,
        "_STUCK_RUNS": metrics.STUCK_RUNS,
        "_SESSION_WRITE_FAILED": metrics.SESSION_WRITE_FAILED,
        "_SESSION_READ_FAILED": metrics.SESSION_READ_FAILED,
        "_TRANSCRIPT_WRITE_FAILED": metrics.TRANSCRIPT_WRITE_FAILED,
        "_WORK_PRODUCT_PARSE_FAILED": metrics.WORK_PRODUCT_PARSE_FAILED,
        "_SANDBOX_TTL_UPSERT_FAILED": metrics.SANDBOX_TTL_UPSERT_FAILED,
        "_SANDBOX_TTL_MISCONFIGURED": metrics.SANDBOX_TTL_MISCONFIGURED,
    }
    for name, expected in mirrored.items():
        assert found.get(name) == expected, (
            f"monitoring_stack.py {name}={found.get(name)!r} drifted from "
            f"ops.metrics value {expected!r} — the alarm would watch a dead metric."
        )


def test_session_alarms_watch_what_the_session_store_really_emits(monkeypatch, capsys, caplog):
    """Every session alarm in the Monitoring stack must match what a real failure emits.

    Drives the real ``FailClosedStorage`` through a failing read and a failing write, and the real
    ``TranscriptRecorder`` through a denied ``CreateEvent``, and checks the captured output against
    the stack's literals: each alarmed ``_SESSION_*`` / ``_TRANSCRIPT_*`` metric name appears in an
    EMF line in the alarms' namespace with the namespace-level ``[]`` dimension set (the alarms
    carry no dimensions), and the ``SessionStorageFailure`` log filter's phrase is in every ERROR
    line. A removed metric (``SessionFenced``) left behind in the stack fails here.
    """
    import asyncio
    import json
    import logging

    from botocore.exceptions import ClientError

    from fake_agentcore import ACCESS_DENIED, MEMORY_ID, FakeAgentCoreClient
    from strandly.memory.session_store import FailClosedStorage, SessionStorageError
    from strandly.memory.transcript import TranscriptRecorder

    stack = _ROOT / "infra" / "stacks" / "monitoring_stack.py"
    found = _module_constants(stack)
    alarmed = {
        v
        for k, v in found.items()
        if k.startswith(("_SESSION_", "_TRANSCRIPT_")) and isinstance(v, str)
    }
    assert alarmed == {
        metrics.SESSION_WRITE_FAILED,
        metrics.SESSION_READ_FAILED,
        metrics.TRANSCRIPT_WRITE_FAILED,
    }
    (phrase,) = [p for name, p, _ in found["_AGENT_LOG_SIGNALS"] if name == "SessionStorageFailure"]

    class _Denied:
        async def read(self, key):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

        async def write(self, key, data):
            raise ClientError({"Error": {"Code": "KMS.AccessDeniedException"}}, "PutObject")

    namespace = "Strandly-prod"  # naming.metrics_namespace, which the deploy sets on the runtime
    monkeypatch.setenv(metrics.NAMESPACE_ENV, namespace)
    store = FailClosedStorage(_Denied(), backend="s3")
    caplog.set_level(logging.ERROR)

    async def _fail_both() -> list[BaseException]:
        raised: list[BaseException] = []
        for call in (store.read("session/s1/snapshot_latest"), store.write("session/s1/x", b"{}")):
            try:
                await call
            except SessionStorageError as e:  # fail closed: both raise
                raised.append(e)
        return raised

    assert len(asyncio.run(_fail_both())) == 2

    # The transcript recorder is fail-open: a denied CreateEvent (not retryable) is logged and
    # counted, and the hook returns normally (the turn goes on).
    memory = FakeAgentCoreClient()
    memory.fail("CreateEvent", ACCESS_DENIED)
    recorder = TranscriptRecorder(memory, memory_id=MEMORY_ID, actor_id="team", session_id="s1")

    class _Added:
        message = {"role": "user", "content": [{"text": "hi"}]}

    asyncio.run(recorder._on_message_added(_Added()))  # type: ignore[arg-type]

    emitted: set[str] = set()
    for line in capsys.readouterr().out.splitlines():
        doc = json.loads(line)
        (cw,) = doc["_aws"]["CloudWatchMetrics"]
        if cw["Namespace"] == namespace and [] in cw["Dimensions"]:
            emitted.update(m["Name"] for m in cw["Metrics"])
    assert alarmed <= emitted, f"alarmed but never emitted: {sorted(alarmed - emitted)}"
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 3 and all(phrase in m for m in errors), errors


def test_sandbox_entrypoint_mirrors_the_mount_path():
    """``sandbox_main.py``'s WORKSPACE must equal ``constants.SANDBOX_MOUNT_PATH``.

    The sandbox runtime's entrypoint deliberately imports nothing from the harness (see its module
    docstring: no Strands SDK in the container the model runs code in, and a sub-30s cold start), so
    the mount path is hand-mirrored. Drift here is a *silent* failure of exactly the kind this file
    exists for: the harness would ``cd`` to a directory the container never created, and every shell
    command in the sandbox would fail with "No such file or directory".

    Read statically because the module imports ``bedrock_agentcore`` (an optional extra).
    """
    found = _module_constants(_SANDBOX_MAIN)
    default = found.get("WORKSPACE")
    if default is None:
        # WORKSPACE is `os.environ.get(..., "/mnt/workspace")` — not a literal — so pull the default
        # out of the source instead of ast.literal_eval-ing the call.
        source = _SANDBOX_MAIN.read_text()
        match = re.search(r'WORKSPACE = os\.environ\.get\([^,]+,\s*"([^"]+)"\)', source)
        assert match, "could not find the WORKSPACE default in sandbox_main.py"
        default = match.group(1)
    assert default == constants.SANDBOX_MOUNT_PATH, (
        f"sandbox_main.py WORKSPACE ({default!r}) has drifted from "
        f"constants.SANDBOX_MOUNT_PATH ({constants.SANDBOX_MOUNT_PATH!r})"
    )


def test_sandbox_entrypoint_mirrors_the_git_credential_protocol():
    """The wake-payload keys must match on both sides of the sandbox boundary.

    This is the credential's *only* delivery channel — it travels in the wake payload precisely
    because AgentCore writes every executed command to CloudWatch — and the two ends cannot share a
    module (``sandbox_main.py`` imports nothing from the harness). A drift here fails in the worst
    available way: the harness sends a credential, the sandbox ignores an unknown key, and the only
    symptom is ``git push`` failing with a 403 while every check reports a healthy session.

    Read statically because the module imports ``bedrock_agentcore`` (an optional extra).
    """
    found = _module_constants(_SANDBOX_MAIN)
    for name, expected in (
        ("GIT_CREDENTIAL_KEY", constants.SANDBOX_GIT_CREDENTIAL_KEY),
        ("GIT_CREDENTIAL_STATUS_KEY", constants.SANDBOX_GIT_CREDENTIAL_STATUS_KEY),
        ("GIT_CREDENTIAL_WRITTEN", constants.SANDBOX_GIT_CREDENTIAL_WRITTEN),
    ):
        assert found.get(name) == expected, (
            f"sandbox_main.py {name}={found.get(name)!r} drifted from {expected!r}; the harness would "
            "send a credential the sandbox never writes."
        )


def test_the_code_interpreter_network_mode_is_knowingly_unmirrored():
    """``common.py`` keeps one constant with no source of truth. Pin *that*, so it can't rot silently.

    ``CODE_INTERPRETER_NETWORK_MODE`` used to mirror the harness; the harness's Code Interpreter
    backend is gone (the sandbox is a second AgentCore Runtime), and the CDK resource it configures is
    still created only because deleting a stateful resource is its own verified change. Dropping the
    name from ``_MIRRORED`` and saying nothing else would leave a constant that *looks* guarded and
    isn't — so this asserts both halves of the current truth: the infra literal exists, and the harness
    has no counterpart. Re-add one and this fails, which is the reminder to mirror it properly.
    """
    assert "CODE_INTERPRETER_NETWORK_MODE" in _module_constants(_COMMON)
    assert not [name for name in dir(constants) if "CODE_INTERPRETER" in name], (
        "the harness defines a Code Interpreter constant again — add it to _MIRRORED and delete this "
        "test, or the CDK resource and the code will drift apart silently."
    )


def test_tail_cursor_mirrors_match_across_the_three_hops():
    """The "attach at the live head" poll cursor exists three times — they must be the same number.

    A poll can ask for the run's live events from a sequence number; a *negative* one means "no
    backlog, just the cursor" (:data:`agentcore_app.TAIL_CURSOR`), which is what the chat panel uses
    when it attaches to a run that was already in flight. The value therefore has to survive three
    hops that cannot import each other: the portal (TypeScript) sends it, the dashboard Lambda
    (standalone bundle) forwards it, the runtime interprets it.

    A drift is silent and its symptom is the bug this exists to prevent: the runtime reads the cursor
    as an ordinary sequence number, replays the whole event buffer, and the panel redraws a finished
    answer as a fresh bubble. (The Lambda used to clamp it to 0, which did exactly that.)
    """
    import importlib

    runtime = importlib.import_module("strandly.serve.agentcore_app").TAIL_CURSOR
    assert runtime < 0, f"agentcore_app.TAIL_CURSOR={runtime} is not a negative sentinel any more"

    lambda_value = _module_constants(_HANDLER).get("TAIL_CURSOR")
    assert lambda_value == runtime, (
        f"dashboard/api/handler.py TAIL_CURSOR={lambda_value!r} drifted from the runtime's "
        f"{runtime!r} — the tail attach would be clamped or rejected on the way through."
    )

    web = (_ROOT / "dashboard" / "web-next" / "src" / "components" / "AgentChat.tsx").read_text()
    js = re.search(r"const\s+TAIL_CURSOR\s*=\s*(-?\d+)\s*;", web)
    assert js, "TAIL_CURSOR not found in dashboard/web-next/src/components/AgentChat.tsx"
    assert int(js.group(1)) == runtime, (
        f"AgentChat.tsx TAIL_CURSOR={js.group(1)} drifted from the runtime's {runtime} — a reattach "
        "would replay the run's whole event buffer into the panel."
    )


def _annotated_constant(path: Path, name: str) -> object:
    """A module-level ``NAME: <type> = <literal>`` (``_module_constants`` reads plain assigns only)."""
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            assert node.value is not None
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


def test_session_retention_surfaces_are_the_id_builders_prefixes():
    """Each per-surface lifecycle rule (``session/<surface>-``) must match ids a builder really
    mints — a ``dash-`` rule once matched nothing while portal sessions got the 90-day default."""
    from strandly.core.session_ids import canonical_session_id
    from strandly.ops.lambdas.slack_ingress.sessions import session_id_for_dm
    from strandly.ops.schedules.model import KIND_RECURRING, SESSION_MODES, Actor, Job
    from strandly_mcp.sessions import default_session_id

    retention = _annotated_constant(_ROOT / "infra" / "stacks" / "common.py", "SESSION_RETENTION_DAYS")
    assert isinstance(retention, dict)
    portal_ts = (_ROOT / "dashboard" / "web-next" / "src" / "lib" / "sessionId.ts").read_text()
    actor = Actor("portal:bob", "dashboard")
    sched = Job(
        id="nightly",
        name="nightly",
        kind=KIND_RECURRING,
        expression="rate(1 day)",
        timezone="UTC",
        prompt="x",
        created_by=actor,
        updated_by=actor,
        created_at="",
        updated_at="",
    )
    when = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for mode in SESSION_MODES:
        assert sched.with_changes(session_mode=mode).session_id(when).startswith("sched-"), mode
    portal_prefix = re.search(r"return `(\w+)-\$\{sanitizeLogin\(login\)\}-`", portal_ts)
    assert portal_prefix, "portal session-id builder changed shape — update this test"
    minted = {
        "slack": session_id_for_dm("T1", "D1", "1791234567.123456"),
        "portal": f"{portal_prefix[1]}-bob-20260105-abcd1234",
        "mcp": default_session_id(),
        "sched": sched.session_id(when),
        "*": canonical_session_id("o/r", "issue", 1),
    }
    surfaces = {k for k in retention if k != "*"}
    assert surfaces == {k for k in minted if k != "*"}, (surfaces, minted)
    for surface, sid in minted.items():
        matching = [s for s in surfaces if sid.startswith(f"{s}-")]
        assert matching == ([] if surface == "*" else [surface]), (surface, sid)


def test_common_invoke_read_timeout_mirrors_the_runtime_client():
    """The CDK sizes the waiting Lambdas' timeouts off the harness's InvokeAgentRuntime read timeout.

    ``common.INVOKE_AGENT_READ_TIMEOUT_S`` is a hand mirror of
    ``ops.runtime_client.INVOKE_READ_TIMEOUT_S`` (the CDK venv can't import the harness); a drift
    would let a Lambda be killed mid-launch on a cold start (no ack, no metrics) or, worse, tempt a
    retry of the non-idempotent launch.
    """
    from strandly.ops import runtime_client

    found = _module_constants(_COMMON)
    assert found.get("INVOKE_AGENT_READ_TIMEOUT_S") == runtime_client.INVOKE_READ_TIMEOUT_S


def test_reconcile_tick_name_mirrors_the_timer_and_is_not_a_job_id():
    """The CDK names the reconcile tick in the jobs group; the harness timer must skip exactly that
    schedule (never list, update or delete it), and no job id may take the name."""
    from strandly.ops.schedules import service, timer

    found = _module_constants(_COMMON)
    assert found.get("JOBS_RECONCILE_SCHEDULE") == timer.RECONCILE_SCHEDULE
    assert not service._ID_RE.match(timer.RECONCILE_SCHEDULE)


def test_session_title_key_prefix_matches():
    """The dashboard's ledger PutItem grant is scoped to the shared title-row key prefix."""
    from strandly.core import session_ids as shared

    found = _module_constants(_COMMON)
    assert found.get("SESSION_TITLE_KEY_PREFIX") == shared.SESSION_TITLE_KEY_PREFIX


def test_slack_thread_link_key_prefix_matches():
    """The Slack Lambdas' ledger grants are scoped to the shared thread-link key prefix."""
    from strandly.ops import slack

    found = _module_constants(_COMMON)
    assert found.get("SLACK_THREAD_LINK_KEY_PREFIX") == slack.THREAD_LINK_KEY_PREFIX


def test_actions_ingress_mirrors_the_runtime_contract():
    """The Actions Lambda and its stack name the same issuer and payload key the runtime trusts."""
    from strandly.core import context
    from strandly.tools import schedule

    actions = _module_constants(_ROOT / "dashboard" / "api" / "actions.py")
    stack = _module_constants(_ROOT / "infra" / "stacks" / "dashboard_stack.py")
    assert actions["ACTIONS_ISSUER"] == stack["ACTIONS_ISSUER"] == context.GITHUB_ACTIONS_ISSUER
    assert actions["GITHUB_ACTIONS_KEY"] == context.GITHUB_ACTIONS_KEY
    assert actions["GITHUB_URL_KEY"] == context.GITHUB_URL_KEY
    assert actions["TRIGGER_PREFIX"] == "github-actions:"
    dashboard = _module_constants(_ROOT / "dashboard" / "api" / "handler.py")
    runtime = _module_constants(_ROOT / "harness" / "src" / "strandly" / "serve" / "agentcore_app.py")
    for name in ("POLL_ISSUED_ONLY_KEY", "POLL_ISSUED_KEY"):
        assert dashboard[name] == runtime[name]
    runtime_stack = (_ROOT / "infra" / "stacks" / "agent_runtime_stack.py").read_text()
    assert f'"{schedule.ENV_ACTIONS_SCHEDULE_REPOSITORIES}"' in runtime_stack


def test_transcript_payloads_live_under_the_stash_prefix_both_readers_are_granted():
    """Oversized transcript payloads share the context stash's prefix, its retention and grants."""
    from strands._context_manager.stash import STASH_PREFIX

    from strandly.core import session_ids as shared

    found = _module_constants(_COMMON)
    assert shared.SESSION_STORE_CONTEXT_PREFIX == constants.SESSION_STORE_CONTEXT_PREFIX == STASH_PREFIX
    assert shared.SESSION_STORE_CONTEXT_PREFIX in found["SESSION_STORE_PREFIXES"]
    assert shared.transcript_payload_key("portal-a", "1-2") == "context/portal-a/transcript/1-2.json"
