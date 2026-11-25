"""Offline S3 for session-store tests: botocore's real error shapes over an in-memory bucket.

``moto`` is not a dependency, so this is a small, faithful fake of the S3 calls the session store
and its readers make (``get_object``, ``put_object``, ``delete_object``, ``delete_objects``,
``list_objects_v2``). It raises **botocore's real exception types**: ``NoSuchKey`` is the class
from a real ``boto3`` S3 client's ``exceptions`` (what ``S3Storage.read`` catches), every injected
failure is a real ``botocore.exceptions.ClientError`` with the wire shape S3 returns. Plug it
into the SDK's real ``S3Storage`` with :func:`s3_storage` and every byte goes through the SDK's
own key mapping, pagination and error handling.

For single-call cases, tests use botocore's ``Stubber`` on :func:`real_s3_client` instead.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

BUCKET = "strandly-test-sessions"


def real_s3_client() -> Any:
    """A real botocore S3 client (dummy credentials, one attempt — Stubber/offline only)."""
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=BotocoreConfig(retries={"max_attempts": 1, "mode": "standard"}),
    )


_REAL = real_s3_client()

#: Error shapes S3 actually returns (code, HTTP status, message).
ACCESS_DENIED = ("AccessDenied", 403, "Access Denied")
KMS_DENIED = (
    "AccessDenied",
    403,
    "User: arn:aws:sts::ACCOUNT:assumed-role/r/s is not authorized to perform: "
    "kms:Decrypt on resource: arn:aws:kms:us-east-1:ACCOUNT:key/k",
)
SLOW_DOWN = ("SlowDown", 503, "Please reduce your request rate.")
INTERNAL = ("InternalError", 500, "We encountered an internal error. Please try again.")


def client_error(shape: tuple[str, int, str], operation: str) -> ClientError:
    code, status, message = shape
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status, "RequestId": "TESTREQUEST"},
        },
        operation,
    )


def no_such_key(key: str) -> Exception:
    exc: Exception = _REAL.exceptions.NoSuchKey(
        {
            "Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist.", "Key": key},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "GetObject",
    )
    return exc


def body(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))


Failure = Callable[[dict[str, Any]], ClientError | None]


class FakeS3Client:
    """An in-memory bucket behind the S3 client surface the session store uses.

    ``fail[op]`` (op = ``"get_object"`` / ``"put_object"`` / ``"delete_object"`` /
    ``"list_objects_v2"``) is a callable taking the call's kwargs and returning a ``ClientError`` to
    raise, or ``None`` to proceed. ``calls`` records ``(op, kwargs)`` for every request.
    """

    exceptions = _REAL.exceptions

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, datetime]] = {}
        self.fail: dict[str, Failure] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # -- helpers ---------------------------------------------------------------------------
    def put(self, key: str, data: bytes, *, modified: datetime | None = None) -> None:
        self.objects[key] = (data, modified or datetime.now(timezone.utc))

    def fail_when(self, op: str, shape: tuple[str, int, str], match: Callable[[str], bool]) -> None:
        operation = "".join(p.title() for p in op.split("_"))

        def _fail(kwargs: dict[str, Any]) -> ClientError | None:
            target = kwargs.get("Key", kwargs.get("Prefix", ""))
            return client_error(shape, operation) if match(target) else None

        self.fail[op] = _fail

    def _check(self, op: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((op, kwargs))
        if kwargs.get("Bucket") != BUCKET:
            raise client_error(("NoSuchBucket", 404, "The specified bucket does not exist"), op)
        failure = self.fail.get(op)
        error = failure(kwargs) if failure else None
        if error is not None:
            raise error

    # -- the S3 API surface ----------------------------------------------------------------
    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self._check("get_object", kwargs)
        key = kwargs["Key"]
        if key not in self.objects:
            raise no_such_key(key)
        data, modified = self.objects[key]
        return {"Body": body(data), "ContentLength": len(data), "LastModified": modified}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self._check("put_object", kwargs)
        data = kwargs["Body"]
        self.put(kwargs["Key"], data if isinstance(data, bytes) else bytes(data))
        return {"ETag": '"etag"'}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self._check("delete_object", kwargs)
        self.objects.pop(kwargs["Key"], None)
        return {}

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self._check("delete_objects", kwargs)
        objects = kwargs["Delete"]["Objects"]
        if len(objects) > 1000:
            raise client_error(("MalformedXML", 400, "more than 1000 keys"), "DeleteObjects")
        for obj in objects:
            self.objects.pop(obj["Key"], None)
        return {} if kwargs["Delete"].get("Quiet") else {"Deleted": [{"Key": o["Key"]} for o in objects]}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        """S3 semantics: UTF-8 binary key order, ``StartAfter`` exclusive, ≤ ``MaxKeys`` (1000)
        per page, an opaque ``ContinuationToken``, ``Delimiter`` roll-up into ``CommonPrefixes``."""
        self._check("list_objects_v2", kwargs)
        prefix = kwargs.get("Prefix", "")
        max_keys = min(int(kwargs.get("MaxKeys", 1000)), 1000)
        after = kwargs.get("ContinuationToken") or kwargs.get("StartAfter") or ""
        delimiter = kwargs.get("Delimiter")
        contents: list[dict[str, Any]] = []
        common: list[str] = []
        last = ""
        truncated = False
        for key in sorted(k for k in self.objects if k.startswith(prefix)):
            if key <= after:
                continue
            if delimiter and delimiter in key[len(prefix):]:
                rolled = key[: len(prefix) + key[len(prefix):].index(delimiter) + 1]
                if rolled in common or rolled <= after:
                    continue
                entry_key = rolled
            else:
                entry_key = key
            if len(contents) + len(common) >= max_keys:
                truncated = True
                break
            if entry_key != key:
                common.append(entry_key)
                last = max(last, entry_key + "\uffff")
            else:
                data, modified = self.objects[key]
                contents.append({"Key": key, "Size": len(data), "LastModified": modified})
                last = key
        out: dict[str, Any] = {
            "IsTruncated": truncated,
            "KeyCount": len(contents) + len(common),
            "MaxKeys": max_keys,
            "Prefix": prefix,
        }
        if contents:
            out["Contents"] = contents
        if common:
            out["CommonPrefixes"] = [{"Prefix": p} for p in common]
        if truncated:
            out["NextContinuationToken"] = last
        return out


def s3_storage(client: Any) -> Any:
    """The SDK's real ``S3Storage`` over ``client`` (a :class:`FakeS3Client` or a stubbed client)."""
    from strands.storage import S3Storage

    storage = S3Storage(BUCKET)
    storage._client = client  # what S3Storage._get_client() returns once built
    return storage
