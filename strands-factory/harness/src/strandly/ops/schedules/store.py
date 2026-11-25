"""The schedules table (DynamoDB): job items (``sk = "job"``), ``rev#n`` revision items, and one
activation sequence item (:data:`QUOTA_KEY`).

Every definition change is one transaction: the job write (conditional on the version and status
the caller read) plus a new revision item. Runtime state (``last_run``, ``fire_window``,
``sync_state``, a completed/expired ``status``) is updated in place without a revision.
"""

from __future__ import annotations

from typing import Any

from strandly.ops.schedules.model import (
    DEFINITION_FIELDS,
    JOB_SK,
    REV_PREFIX,
    STATUS_ACTIVE,
    Job,
    LastRun,
    Revision,
)

# Fields an edit may set; runtime state is left alone.
_EDIT_FIELDS = (*DEFINITION_FIELDS, "enabled", "updated_by", "updated_at", "version", "sync_state")
_OPTIONAL_EDIT_FIELDS = ("skill", "notify", "starts_at", "expires_at", "pending_trigger")
# The item every write that makes a job active advances in its transaction, conditional on the value
# read before counting active jobs, so two concurrent activations cannot both pass the team's cap.
# ``#`` never starts a job id, and the job scan's ``sk`` filter skips it.
QUOTA_KEY = ("#team", "active-seq")


class ConflictError(Exception):
    """A conditional write lost: the job changed (or exists / is gone) since it was read."""


def _codec() -> tuple[Any, Any]:
    from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

    return TypeSerializer(), TypeDeserializer()


def _plain(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _is_conditional_failure(error: BaseException) -> bool:
    code = getattr(error, "response", {}).get("Error", {}).get("Code", "")
    return code in ("ConditionalCheckFailedException", "TransactionCanceledException")


class _Expr:
    """Builds expression attribute names/values without colliding with reserved words."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.values: dict[str, Any] = {}

    def name(self, field: str) -> str:
        key = f"#{field}"
        self.names[key] = field
        return key

    def value(self, label: str, value: Any) -> str:
        key = f":{label}"
        self.values[key] = value
        return key


class JobStore:
    """DynamoDB access for jobs and their revisions (``client`` is a low-level boto3 client)."""

    def __init__(self, client: Any, table: str) -> None:
        self._client = client
        self._table = table
        self._ser, self._de = _codec()

    def _encode(self, item: dict[str, Any]) -> dict[str, Any]:
        return {k: self._ser.serialize(v) for k, v in item.items()}

    def _decode(self, item: dict[str, Any]) -> dict[str, Any]:
        return {k: _plain(self._de.deserialize(v)) for k, v in item.items()}

    def _key(self, job_id: str, sk: str = JOB_SK) -> dict[str, Any]:
        return self._encode({"id": job_id, "sk": sk})

    def _values(self, expr: _Expr) -> dict[str, Any]:
        return self._encode(expr.values)

    # --- reads ------------------------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        resp = self._client.get_item(
            TableName=self._table, Key=self._key(job_id), ConsistentRead=True
        )
        item = resp.get("Item")
        return Job.from_item(self._decode(item)) if item else None

    def list_jobs(self, *, consistent: bool = False) -> list[Job]:
        """Every job item. The scan reads (and pays for) the revision items too; the filter only
        drops them from the response, which is fine at this table's size."""
        jobs: list[Job] = []
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "FilterExpression": "#sk = :job",
            "ExpressionAttributeNames": {"#sk": "sk"},
            "ExpressionAttributeValues": self._encode({":job": JOB_SK}),
        }
        if consistent:
            kwargs["ConsistentRead"] = True
        while True:
            resp = self._client.scan(**kwargs)
            jobs.extend(Job.from_item(self._decode(i)) for i in resp.get("Items", []))
            if not resp.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return sorted(jobs, key=lambda j: j.id)

    def revisions(self, job_id: str, *, newest_first: bool = True, limit: int | None = None) -> list[Revision]:
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "KeyConditionExpression": "#id = :id AND begins_with(#sk, :rev)",
            "ExpressionAttributeNames": {"#id": "id", "#sk": "sk"},
            "ExpressionAttributeValues": self._encode({":id": job_id, ":rev": REV_PREFIX}),
            "ScanIndexForward": not newest_first,
            "ConsistentRead": True,
        }
        if limit:
            kwargs["Limit"] = limit
        revs: list[Revision] = []
        while True:
            resp = self._client.query(**kwargs)
            revs.extend(Revision.from_item(self._decode(i)) for i in resp.get("Items", []))
            if (limit and len(revs) >= limit) or not resp.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return revs[:limit] if limit else revs

    def latest_rev(self, job_id: str) -> int:
        revs = self.revisions(job_id, limit=1)
        return revs[0].rev if revs else 0

    def activation_seq(self) -> int:
        """The activation sequence; read it before counting active jobs for the team's cap."""
        resp = self._client.get_item(
            TableName=self._table, Key=self._key(*QUOTA_KEY), ConsistentRead=True
        )
        item = resp.get("Item")
        return int(self._decode(item).get("seq", 0)) if item else 0

    # --- definition writes (job + revision, one transaction) -------------------------------

    def _put_rev(self, revision: Revision) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self._table,
                "Item": self._encode(revision.to_item()),
                "ConditionExpression": "attribute_not_exists(#id)",
                "ExpressionAttributeNames": {"#id": "id"},
            }
        }

    def _advance(self, seen: int) -> dict[str, Any]:
        """Advance the activation sequence from ``seen``; the transaction fails if it moved."""
        item = {"id": QUOTA_KEY[0], "sk": QUOTA_KEY[1], "seq": seen + 1}
        return {
            "Put": {
                "TableName": self._table,
                "Item": self._encode(item),
                "ConditionExpression": "attribute_not_exists(#id) OR #seq = :seen",
                "ExpressionAttributeNames": {"#id": "id", "#seq": "seq"},
                "ExpressionAttributeValues": self._encode({":seen": seen}),
            }
        }

    def _transact(self, items: list[dict[str, Any]]) -> None:
        try:
            self._client.transact_write_items(TransactItems=items)
        except Exception as e:
            if _is_conditional_failure(e):
                raise ConflictError(str(e)) from e
            raise

    def create(self, job: Job, revision: Revision, *, activation: int | None = None) -> None:
        """Write a new job; ``activation`` (the sequence read before the cap check) also advances
        the activation sequence."""
        put_job = {
            "Put": {
                "TableName": self._table,
                "Item": self._encode(job.to_item()),
                "ConditionExpression": "attribute_not_exists(#id)",
                "ExpressionAttributeNames": {"#id": "id"},
            }
        }
        extra = [] if activation is None else [self._advance(activation)]
        self._transact([put_job, self._put_rev(revision), *extra])

    def update(
        self,
        job: Job,
        revision: Revision,
        *,
        expected_version: int,
        expected_status: str,
        clear_window: bool = False,
        activation: int | None = None,
    ) -> None:
        """Write an edit; ``clear_window`` also forgets the last fire window taken, and
        ``activation`` advances the activation sequence as :meth:`create` does."""
        item = job.to_item()
        expr = _Expr()
        sets = [f"{expr.name(f)} = {expr.value(f, item[f])}" for f in _EDIT_FIELDS if f in item]
        removes = [expr.name(f) for f in _OPTIONAL_EDIT_FIELDS if f not in item]
        if clear_window:
            removes.append(expr.name("fire_window"))
        update = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
        condition = (
            f"attribute_exists({expr.name('id')}) AND {expr.name('version')} = "
            f"{expr.value('expected_version', expected_version)} AND {expr.name('status')} = "
            f"{expr.value('expected_status', expected_status)}"
        )
        update_job = {
            "Update": {
                "TableName": self._table,
                "Key": self._key(job.id),
                "UpdateExpression": update,
                "ConditionExpression": condition,
                "ExpressionAttributeNames": expr.names,
                "ExpressionAttributeValues": self._values(expr),
            }
        }
        extra = [] if activation is None else [self._advance(activation)]
        self._transact([update_job, self._put_rev(revision), *extra])

    def delete(self, job_id: str, revision: Revision, *, expected_version: int) -> None:
        delete_job = {
            "Delete": {
                "TableName": self._table,
                "Key": self._key(job_id),
                "ConditionExpression": "#version = :v",
                "ExpressionAttributeNames": {"#version": "version"},
                "ExpressionAttributeValues": self._encode({":v": expected_version}),
            }
        }
        self._transact([delete_job, self._put_rev(revision)])

    # --- runtime state ----------------------------------------------------------------------

    def _update(self, job_id: str, update: str, condition: str, expr: _Expr) -> bool:
        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(job_id),
                UpdateExpression=update,
                ConditionExpression=condition,
                ExpressionAttributeNames=expr.names,
                ExpressionAttributeValues=self._values(expr),
            )
        except Exception as e:
            if _is_conditional_failure(e):
                return False
            raise
        return True

    def set_sync(self, job_id: str, *, version: int, state: str, error: str | None = None) -> bool:
        """Record the Scheduler sync outcome, unless the job has been edited since."""
        expr = _Expr()
        update = f"SET {expr.name('sync_state')} = {expr.value('state', state)}"
        if error:
            update += f", {expr.name('sync_error')} = {expr.value('error', error[:500])}"
        else:
            update += f" REMOVE {expr.name('sync_error')}"
        condition = f"{expr.name('version')} = {expr.value('v', version)}"
        return self._update(job_id, update, condition, expr)

    def claim(self, job_id: str, *, window: str, last_run: LastRun) -> bool:
        """Take the fire ``window`` for an active job; ``False`` if the job is not active or the
        last window taken is not earlier (so a window dispatches at most once, and a late
        redelivery of an older one not at all). Windows of one schedule sort in time order."""
        expr = _Expr()
        fw = expr.name("fire_window")
        update = (
            f"SET {fw} = {expr.value('w', window)}, "
            f"{expr.name('last_run')} = {expr.value('lr', last_run.to_item())}"
        )
        condition = (
            f"attribute_exists({expr.name('id')}) AND {expr.name('status')} = "
            f"{expr.value('active', STATUS_ACTIVE)} AND "
            f"(attribute_not_exists({fw}) OR {fw} < :w)"
        )
        return self._update(job_id, update, condition, expr)

    def record_run(self, job_id: str, last_run: LastRun) -> bool:
        expr = _Expr()
        update = f"SET {expr.name('last_run')} = {expr.value('lr', last_run.to_item())}"
        return self._update(job_id, update, f"attribute_exists({expr.name('id')})", expr)

    def set_status(
        self, job_id: str, status: str, *, expected: tuple[str, ...], version: int
    ) -> bool:
        """Move a job to a terminal runtime status (completed / expired) without a revision,
        unless it was edited since ``version`` was read."""
        expr = _Expr()
        st = expr.name("status")
        update = (
            f"SET {st} = {expr.value('status', status)}, "
            f"{expr.name('enabled')} = {expr.value('enabled', status == STATUS_ACTIVE)}"
        )
        allowed = ", ".join(expr.value(f"e{i}", s) for i, s in enumerate(expected))
        condition = (
            f"attribute_exists({expr.name('id')}) AND {st} IN ({allowed}) AND "
            f"{expr.name('version')} = {expr.value('v', version)}"
        )
        return self._update(job_id, update, condition, expr)

    def claim_manual_run(self, job_id: str, *, at: str, not_since: str) -> bool:
        """Record a manual run at ``at``; ``False`` if one was recorded at or after ``not_since``
        (ISO UTC strings sort in time order)."""
        expr = _Expr()
        mr = expr.name("manual_run_at")
        update = f"SET {mr} = {expr.value('at', at)}"
        condition = (
            f"attribute_exists({expr.name('id')}) AND "
            f"(attribute_not_exists({mr}) OR {mr} < {expr.value('since', not_since)})"
        )
        return self._update(job_id, update, condition, expr)
