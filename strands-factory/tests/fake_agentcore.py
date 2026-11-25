"""An in-memory AgentCore Memory data plane that speaks the real API shapes.

``FakeAgentCoreClient`` implements the ``bedrock-agentcore`` operations Strandly calls
(``create_event``, ``list_events``, ``get_event``, ``batch_create_memory_records``,
``retrieve_memory_records``, ``list_memory_records``). Every request is validated against the
installed botocore service model (``ParamValidator`` — the check botocore runs before sending, so
a wrong field, a too-long string or a bad session-id pattern fails here exactly as it would against
the service), and every response is validated against the operation's output shape. Errors are
botocore's own modeled exception classes, built with the service's error response shape.

Service behaviour the fake assumes (and the report lists as a dev-environment check): ``ListEvents``
returns a session's events newest first; records written with ``BatchCreateMemoryRecords`` are
searchable at once in their namespace.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

MEMORY_ID = "strandly_test_memory-abcdefghij"
CREATE_EVENT_MAX_PAYLOAD_BYTES = 102_400


def real_agentcore_client() -> Any:
    """A real ``bedrock-agentcore`` client (dummy credentials) for ``Stubber`` tests."""
    return boto3.client(
        "bedrock-agentcore",
        region_name="us-west-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=BotocoreConfig(retries={"max_attempts": 1, "mode": "standard"}),
    )


_REAL = real_agentcore_client()
_MODEL = _REAL.meta.service_model
_VALIDATOR = ParamValidator()

THROTTLED = ("ThrottlingException", 429, "Rate exceeded")
INTERNAL = ("InternalServerException", 500, "Internal server error")
ACCESS_DENIED = ("AccessDeniedException", 403, "User is not authorized to perform this action")
VALIDATION = ("ValidationException", 400, "1 validation error detected")
NOT_FOUND = ("ResourceNotFoundException", 404, "Memory not found")


def client_error(shape: tuple[str, int, str], operation: str) -> ClientError:
    code, status, message = shape
    response = {
        "Error": {"Code": code, "Message": message},
        "ResponseMetadata": {"HTTPStatusCode": status, "RequestId": "TESTREQUEST"},
    }
    cls = getattr(_REAL.exceptions, code, ClientError)
    error: ClientError = cls(response, operation)
    return error


def _validate(operation: str, params: dict[str, Any], *, output: bool = False) -> None:
    op = _MODEL.operation_model(operation)
    shape = op.output_shape if output else op.input_shape
    report = _VALIDATOR.validate(params, shape)
    if report.has_errors():
        if output:
            raise AssertionError(f"fake {operation} produced an invalid response: {report.generate_report()}")
        raise client_error(("ValidationException", 400, report.generate_report()), operation)


def _words(text: str) -> set[str]:
    return {w for w in "".join(c.lower() if c.isalnum() else " " for c in text).split() if len(w) > 2}


class FakeAgentCoreClient:
    """In-memory events and records keyed exactly as the service keys them."""

    def __init__(self) -> None:
        self.events: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        self.records: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._failures: list[tuple[str, Callable[[dict[str, Any]], bool], tuple[str, int, str], int]] = []
        self._ids = itertools.count(1)

    # -- failure injection -------------------------------------------------------------------
    def fail(
        self,
        operation: str,
        shape: tuple[str, int, str],
        *,
        times: int = 1_000_000,
        when: Callable[[dict[str, Any]], bool] = lambda _p: True,
    ) -> None:
        self._failures.append((operation, when, shape, times))

    def _maybe_fail(self, operation: str, params: dict[str, Any]) -> None:
        for i, (op, when, shape, times) in enumerate(self._failures):
            if op == operation and times > 0 and when(params):
                self._failures[i] = (op, when, shape, times - 1)
                raise client_error(shape, operation)

    def _call(self, operation: str, params: dict[str, Any]) -> None:
        self.calls.append((operation, params))
        _validate(operation, params)
        self._maybe_fail(operation, params)

    def _respond(self, operation: str, response: dict[str, Any]) -> dict[str, Any]:
        _validate(operation, response, output=True)
        return response

    # -- events ------------------------------------------------------------------------------
    def create_event(self, **params: Any) -> dict[str, Any]:
        self._call("CreateEvent", params)
        # The service's own check, seen in prod: "JSON payload size of N bytes exceeds the maximum
        # allowed size of 102400 bytes". Measured on compact, ASCII-escaped JSON.
        size = len(json.dumps(params["payload"], separators=(",", ":"), default=str).encode())
        if size > CREATE_EVENT_MAX_PAYLOAD_BYTES:
            raise client_error(
                (
                    "ValidationException",
                    400,
                    f"JSON payload size of {size} bytes exceeds the maximum allowed size of "
                    f"{CREATE_EVENT_MAX_PAYLOAD_BYTES} bytes",
                ),
                "CreateEvent",
            )
        n = next(self._ids)
        ts = params["eventTimestamp"]
        event = {
            "memoryId": params["memoryId"],
            "actorId": params["actorId"],
            "sessionId": params["sessionId"],
            "eventId": f"{int(ts.timestamp() * 1000):013d}#{hashlib.sha1(str(n).encode()).hexdigest()[:8]}",
            "eventTimestamp": ts,
            "payload": params["payload"],
        }
        if params.get("metadata"):
            event["metadata"] = params["metadata"]
        if params.get("extractionMode"):
            event["_extractionMode"] = params["extractionMode"]  # test-only: what the call asked
        key = (params["memoryId"], params["actorId"], params["sessionId"])
        self.events.setdefault(key, []).append(event)
        public = {k: v for k, v in event.items() if not k.startswith("_")}
        return self._respond("CreateEvent", {"event": public})

    def session_events(self, actor: str, session: str, memory_id: str = MEMORY_ID) -> list[dict[str, Any]]:
        return list(self.events.get((memory_id, actor, session), []))

    def list_events(self, **params: Any) -> dict[str, Any]:
        self._call("ListEvents", params)
        stored = self.events.get((params["memoryId"], params["actorId"], params["sessionId"]), [])
        newest_first = sorted(stored, key=lambda e: e["eventTimestamp"], reverse=True)
        start = int(params.get("nextToken") or 0)
        size = int(params.get("maxResults") or 20)
        page = newest_first[start : start + size]
        out = []
        for ev in page:
            item = {k: v for k, v in ev.items() if not k.startswith("_")}
            if not params.get("includePayloads", True):
                item["payload"] = []
            out.append(item)
        response: dict[str, Any] = {"events": out}
        if start + size < len(newest_first):
            response["nextToken"] = str(start + size)
        return self._respond("ListEvents", response)

    # -- records -----------------------------------------------------------------------------
    def batch_create_memory_records(self, **params: Any) -> dict[str, Any]:
        self._call("BatchCreateMemoryRecords", params)
        ok = []
        for rec in params["records"]:
            record_id = f"mem-{hashlib.sha256(str(next(self._ids)).encode()).hexdigest()[:40]}"
            stored = {
                "memoryRecordId": record_id,
                "content": rec["content"],
                "memoryStrategyId": rec.get("memoryStrategyId", "direct"),
                "namespaces": rec["namespaces"],
                "createdAt": datetime.now(timezone.utc),
                "timestamp": rec["timestamp"],
            }
            if rec.get("metadata"):
                stored["metadata"] = rec["metadata"]
            self.records.append(stored)
            ok.append(
                {"memoryRecordId": record_id, "status": "SUCCEEDED", "requestIdentifier": rec["requestIdentifier"]}
            )
        return self._respond("BatchCreateMemoryRecords", {"successfulRecords": ok, "failedRecords": []})

    def _in_scope(self, record: dict[str, Any], params: dict[str, Any]) -> bool:
        namespaces = record.get("namespaces") or []
        if "namespace" in params:
            return params["namespace"] in namespaces
        return any(ns.startswith(params["namespacePath"]) for ns in namespaces)

    def retrieve_memory_records(self, **params: Any) -> dict[str, Any]:
        self._call("RetrieveMemoryRecords", params)
        query = _words(params["searchCriteria"]["searchQuery"])
        scored = []
        for rec in self.records:
            if not self._in_scope(rec, params):
                continue
            overlap = len(query & _words(rec["content"]["text"]))
            if overlap:
                scored.append((overlap / max(len(query), 1), rec))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        top = scored[: params["searchCriteria"].get("topK", 10)]
        return self._respond(
            "RetrieveMemoryRecords",
            {"memoryRecordSummaries": [{**rec, "score": min(score, 1.0)} for score, rec in top]},
        )

    def list_memory_records(self, **params: Any) -> dict[str, Any]:
        self._call("ListMemoryRecords", params)
        matched = [r for r in self.records if self._in_scope(r, params)]
        for flt in params.get("metadataFilters") or []:
            key = flt["left"]["metadataKey"]
            if flt["operator"] == "EQUALS_TO":
                want = flt["right"]["metadataValue"]
                matched = [r for r in matched if (r.get("metadata") or {}).get(key) == want]
            elif flt["operator"] == "EXISTS":
                matched = [r for r in matched if key in (r.get("metadata") or {})]
        start = int(params.get("nextToken") or 0)
        size = int(params.get("maxResults") or 20)
        response: dict[str, Any] = {"memoryRecordSummaries": matched[start : start + size]}
        if start + size < len(matched):
            response["nextToken"] = str(start + size)
        return self._respond("ListMemoryRecords", response)
