"""In-memory DynamoDB and EventBridge Scheduler clients for the schedules tests.

The DynamoDB fake speaks the low-level (typed) API and evaluates the condition and update
expressions ``ops/schedules/store.py`` writes, so the real ``JobStore`` runs against it.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

_SER, _DE = TypeSerializer(), TypeDeserializer()
_TOKEN = re.compile(r"\s*(<>|<=|>=|<|>|=|\(|\)|,|[#:][A-Za-z0-9_]+|[A-Za-z_]+)")
_MISSING = object()


def client_error(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


def _plain(typed: dict[str, Any]) -> dict[str, Any]:
    return {k: _DE.deserialize(v) for k, v in typed.items()}


class _Cond:
    """Recursive-descent evaluator for the condition expressions the store uses."""

    def __init__(self, text: str, names: dict[str, str], values: dict[str, Any]) -> None:
        self.tokens = _TOKEN.findall(text)
        assert "".join(self.tokens).replace(" ", "") == text.replace(" ", ""), text
        self.pos = 0
        self.names = names
        self.values = values

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, expect: str | None = None) -> str:
        tok = self.tokens[self.pos]
        if expect is not None:
            assert tok == expect, (tok, expect)
        self.pos += 1
        return tok

    def evaluate(self, item: dict[str, Any] | None) -> bool:
        self.item = item or {}
        result = self.expr()
        assert self.peek() is None
        return result

    def expr(self) -> bool:
        result = self.and_expr()
        while self.peek() == "OR":
            self.take()
            rhs = self.and_expr()
            result = result or rhs
        return result

    def and_expr(self) -> bool:
        result = self.term()
        while self.peek() == "AND":
            self.take()
            rhs = self.term()
            result = result and rhs
        return result

    def operand(self) -> Any:
        tok = self.take()
        if tok.startswith("#"):
            return self.item.get(self.names[tok], _MISSING)
        return self.values[tok]

    def term(self) -> bool:
        tok = self.peek()
        if tok == "(":
            self.take()
            result = self.expr()
            self.take(")")
            return result
        if tok in ("attribute_exists", "attribute_not_exists"):
            self.take()
            self.take("(")
            name = self.names[self.take()]
            self.take(")")
            return (name in self.item) == (tok == "attribute_exists")
        if tok == "begins_with":
            self.take()
            self.take("(")
            value = self.operand()
            self.take(",")
            prefix = self.operand()
            self.take(")")
            return isinstance(value, str) and isinstance(prefix, str) and value.startswith(prefix)
        lhs = self.operand()
        op = self.take()
        if op == "IN":
            self.take("(")
            options = [self.operand()]
            while self.peek() == ",":
                self.take()
                options.append(self.operand())
            self.take(")")
            return lhs in options
        rhs = self.operand()
        if lhs is _MISSING or rhs is _MISSING:
            return False  # DynamoDB: any comparison with a missing attribute is false
        if op in ("<", "<=", ">", ">="):
            assert type(lhs) is type(rhs), (lhs, rhs)
        return {
            "=": lambda: lhs == rhs,
            "<>": lambda: lhs != rhs,
            "<": lambda: lhs < rhs,
            "<=": lambda: lhs <= rhs,
            ">": lambda: lhs > rhs,
            ">=": lambda: lhs >= rhs,
        }[op]()


class FakeDynamoDB:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}  # plain values
        self.calls: list[str] = []

    @staticmethod
    def _key(key: dict[str, Any]) -> tuple[str, str]:
        plain = _plain(key)
        return str(plain["id"]), str(plain["sk"])

    def _check(self, req: dict[str, Any], current: dict[str, Any] | None) -> bool:
        cond = req.get("ConditionExpression")
        if not cond:
            return True
        values = _plain(req.get("ExpressionAttributeValues", {}))
        return _Cond(cond, req.get("ExpressionAttributeNames", {}), values).evaluate(current)

    def _apply_update(self, req: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        names = req.get("ExpressionAttributeNames", {})
        values = _plain(req.get("ExpressionAttributeValues", {}))
        item = copy.deepcopy(current)
        text = req["UpdateExpression"]
        set_part, _, remove_part = text.partition(" REMOVE ")
        if set_part.startswith("SET "):
            for assignment in set_part[4:].split(","):
                lhs, _, rhs = assignment.partition("=")
                item[names[lhs.strip()]] = copy.deepcopy(values[rhs.strip()])
        elif set_part.startswith("REMOVE "):
            remove_part = set_part[7:]
        for name in filter(None, (n.strip() for n in remove_part.split(","))):
            item.pop(names[name], None)
        return item

    def get_item(self, **req: Any) -> dict[str, Any]:
        self.calls.append("get_item" + ("[consistent]" if req.get("ConsistentRead") else ""))
        item = self.items.get(self._key(req["Key"]))
        return {"Item": {k: _SER.serialize(v) for k, v in item.items()}} if item else {}

    def scan(self, **req: Any) -> dict[str, Any]:
        self.calls.append("scan" + ("[consistent]" if req.get("ConsistentRead") else ""))
        out = [i for i in self.items.values() if self._check({**req, "ConditionExpression": req.get("FilterExpression")}, i)]
        return {"Items": [{k: _SER.serialize(v) for k, v in i.items()} for i in out]}

    def query(self, **req: Any) -> dict[str, Any]:
        self.calls.append("query")
        cond = {**req, "ConditionExpression": req["KeyConditionExpression"]}
        out = sorted(
            (i for i in self.items.values() if self._check(cond, i)),
            key=lambda i: i["sk"],
            reverse=not req.get("ScanIndexForward", True),
        )
        if req.get("Limit"):
            out = out[: req["Limit"]]
        return {"Items": [{k: _SER.serialize(v) for k, v in i.items()} for i in out]}

    def update_item(self, **req: Any) -> dict[str, Any]:
        self.calls.append("update_item")
        key = self._key(req["Key"])
        current = self.items.get(key)
        if not self._check(req, current):
            raise client_error("ConditionalCheckFailedException", "UpdateItem")
        self.items[key] = self._apply_update(req, current or _plain(req["Key"]))
        return {}

    def transact_write_items(self, TransactItems: list[dict[str, Any]]) -> dict[str, Any]:  # noqa: N803
        self.calls.append("transact_write_items")
        staged: list[tuple[str, tuple[str, str], dict[str, Any]]] = []
        for entry in TransactItems:
            (op, req), = entry.items()
            key = self._key(req["Item"] if op == "Put" else req["Key"])
            if not self._check(req, self.items.get(key)):
                raise client_error("TransactionCanceledException", "TransactWriteItems")
            staged.append((op, key, req))
        for op, key, req in staged:
            if op == "Put":
                self.items[key] = _plain(req["Item"])
            elif op == "Delete":
                self.items.pop(key, None)
            else:
                self.items[key] = self._apply_update(req, self.items.get(key) or _plain(req["Key"]))
        return {}


class FakeScheduler:
    """Keeps schedules per (group, name) and rejects what the real API rejects: a ``StartDate``
    more than 5 minutes ago, ``StartDate >= EndDate``, an ``at()`` in the past."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.schedules: dict[tuple[str, str], dict[str, Any]] = {}
        self.fail: set[str] = set()
        self.fail_times: dict[str, int] = {}  # op -> fail this many more calls
        self.calls: list[tuple[str, str]] = []
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail:
            raise client_error("InternalServerException", op)
        if self.fail_times.get(op, 0) > 0:
            self.fail_times[op] -= 1
            raise client_error("InternalServerException", op)

    def _validate(self, req: dict[str, Any], op: str) -> None:
        now = self.clock()
        start, end = req.get("StartDate"), req.get("EndDate")
        expr = req["ScheduleExpression"]
        if expr.startswith("at("):
            tz = req.get("ScheduleExpressionTimezone") or "UTC"
            at = datetime.strptime(expr[3:-1], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc if tz == "UTC" else ZoneInfo(tz)
            )
            if at <= now:
                raise client_error("ValidationException", op)
            return
        if start is not None and start < now - timedelta(minutes=5):
            raise client_error("ValidationException", op)
        if start is not None and end is not None and start >= end:
            raise client_error("ValidationException", op)

    def create_schedule(self, **req: Any) -> dict[str, Any]:
        self.calls.append(("create", req["Name"]))
        self._maybe_fail("create_schedule")
        self._validate(req, "CreateSchedule")
        key = (req["GroupName"], req["Name"])
        if key in self.schedules:
            raise client_error("ConflictException", "CreateSchedule")
        self.schedules[key] = copy.deepcopy(req)
        return {"ScheduleArn": f"arn:schedule/{req['GroupName']}/{req['Name']}"}

    def update_schedule(self, **req: Any) -> dict[str, Any]:
        self.calls.append(("update", req["Name"]))
        self._maybe_fail("update_schedule")
        key = (req["GroupName"], req["Name"])
        if key not in self.schedules:
            raise client_error("ResourceNotFoundException", "UpdateSchedule")
        self._validate(req, "UpdateSchedule")
        self.schedules[key] = copy.deepcopy(req)
        return {}

    def delete_schedule(self, Name: str, GroupName: str) -> dict[str, Any]:  # noqa: N803
        self.calls.append(("delete", Name))
        self._maybe_fail("delete_schedule")
        if (GroupName, Name) not in self.schedules:
            raise client_error("ResourceNotFoundException", "DeleteSchedule")
        del self.schedules[(GroupName, Name)]
        return {}

    def get_schedule(self, Name: str, GroupName: str) -> dict[str, Any]:  # noqa: N803
        self._maybe_fail("get_schedule")
        if (GroupName, Name) not in self.schedules:
            raise client_error("ResourceNotFoundException", "GetSchedule")
        return copy.deepcopy(self.schedules[(GroupName, Name)])

    def list_schedules(self, GroupName: str, NextToken: str | None = None) -> dict[str, Any]:  # noqa: N803
        self._maybe_fail("list_schedules")
        return {"Schedules": [{"Name": n} for g, n in self.schedules if g == GroupName]}
