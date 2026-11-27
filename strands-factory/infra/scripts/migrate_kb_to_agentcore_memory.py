"""One-off: copy the Bedrock knowledge base's long-term memories into AgentCore Memory records.

Reads the S3 Vectors index behind the knowledge base (``list-vectors --return-metadata``, paged, read
only) or a dump of it (``--from-file``), and writes one AgentCore Memory long-term record per chunk
with ``BatchCreateMemoryRecords``:

- a chunk without ``namespace`` metadata is team memory → ``/facts/team/``;
- ``namespace=slack-user-<UID>`` is that person's memory → ``/facts/slack-user-<UID>/``.

Chunks are NOT reassembled into whole documents: the index stores no chunk order, and only a
minority of multi-chunk documents have a detectable overlap to stitch by, so a reassembly would be
a guess. Each chunk is self-contained text the knowledge base retrieved on its own, so one record
per chunk keeps recall exactly as granular as before. Exact duplicates (same scope, same text) are
dropped. A chunk over the 16,000-character record limit is split (none are, in practice).

Every record carries metadata ``origin=kb-migration``, ``migration_id`` (= its
``requestIdentifier``, derived from the source document id and the chunk text, so it is stable
across runs) and ``kb_doc`` (a hash of the source document id); ``timestamp`` is the chunk's
original ``createDate``.

Modes (default ``--dry-run``: reads only, writes nothing):

- ``--dry-run`` prints counts per scope, records, splits, duplicates and rejects.
- ``--apply --memory-id ID --yes`` writes. **Resumable**: it first lists the records already in
  each target namespace and skips every ``migration_id`` already there, so a re-run after an
  interruption (or a throttling storm) writes only what is missing. Each batch also sends a
  ``clientToken`` derived from its record ids, so a retried call is idempotent. Throttling and
  server errors back off exponentially.
- ``--verify --memory-id ID`` (also run after ``--apply``) counts the migrated records per namespace
  with ``ListMemoryRecords`` and exits non-zero unless every namespace holds what the plan expects.

Personal namespaces carry Slack user ids; the output masks them unless ``--show-namespaces``.

Run from the repo root with the project venv, e.g.::

    uv run --no-sync python infra/scripts/migrate_kb_to_agentcore_memory.py \\
        --vector-bucket <bucket> --index <index> --region <region> --profile <profile>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TEAM_NAMESPACE = "/facts/team/"
PERSONAL_PREFIX = "slack-user-"
ORIGIN = "kb-migration"
RECORD_MAX_CHARS = 16_000
BATCH_SIZE = 100
LIST_PAGE = 100
_PERSONAL_RE = re.compile(r"^slack-user-[A-Z0-9]{2,32}$")
_RETRYABLE = frozenset(
    {
        "ThrottlingException",
        "ThrottledException",
        "ServiceQuotaExceededException",
        "InternalServerException",
        "ServiceException",
        "RetryableConflictException",
        "ServiceUnavailableException",
    }
)


# --- reading the index -------------------------------------------------------------------------


def vectors_from_file(path: Path) -> Iterator[dict[str, Any]]:
    """Vectors from a dump: one ``list-vectors`` response, a list of them, a list of vectors, or JSONL."""
    raw = path.read_text()
    try:
        docs: list[Any] = [json.loads(raw)]
    except json.JSONDecodeError:
        docs = [json.loads(line) for line in raw.splitlines() if line.strip()]
    for doc in docs:
        pages = doc if isinstance(doc, list) else [doc]
        for page in pages:
            if isinstance(page, dict) and isinstance(page.get("vectors"), list):
                yield from page["vectors"]
            elif isinstance(page, dict) and "key" in page:
                yield page


def vectors_from_index(client: Any, bucket: str, index: str) -> Iterator[dict[str, Any]]:
    """Every vector of the index with its metadata (``ListVectors``, paged, read only)."""
    token: str | None = None
    while True:
        params: dict[str, Any] = {
            "vectorBucketName": bucket,
            "indexName": index,
            "returnMetadata": True,
            "returnData": False,
            "maxResults": 1000,
        }
        if token:
            params["nextToken"] = token
        page = client.list_vectors(**params)
        yield from page.get("vectors") or []
        token = page.get("nextToken")
        if not token:
            return


# --- the plan ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    request_id: str
    namespace: str
    text: str
    timestamp: datetime
    doc_hash: str

    def request(self) -> dict[str, Any]:
        return {
            "requestIdentifier": self.request_id,
            "namespaces": [self.namespace],
            "content": {"text": self.text},
            "timestamp": self.timestamp,
            "metadata": {
                "origin": {"stringValue": ORIGIN},
                "migration_id": {"stringValue": self.request_id},
                "kb_doc": {"stringValue": self.doc_hash},
            },
        }


@dataclass
class Plan:
    records: list[Record] = field(default_factory=list)
    vectors: int = 0
    documents: int = 0
    multi_chunk_documents: int = 0
    duplicates: int = 0
    splits: int = 0
    rejects: Counter[str] = field(default_factory=Counter)

    def per_namespace(self) -> Counter[str]:
        return Counter(r.namespace for r in self.records)


def _sha(text: str, n: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _namespace(scope: Any) -> str | None:
    if scope is None or scope == "":
        return TEAM_NAMESPACE
    if isinstance(scope, str) and _PERSONAL_RE.match(scope):
        return f"/facts/{scope}/"
    return None


def _parts(text: str) -> list[str]:
    return [text[i : i + RECORD_MAX_CHARS] for i in range(0, len(text), RECORD_MAX_CHARS)]


def build_plan(vectors: Iterable[dict[str, Any]]) -> Plan:
    """Turn index vectors into the records to write (deterministic, whatever the listing order)."""
    plan = Plan()
    by_doc: dict[tuple[str, str], list[tuple[str, datetime]]] = defaultdict(list)
    for vector in vectors:
        plan.vectors += 1
        meta = vector.get("metadata") if isinstance(vector, dict) else None
        if not isinstance(meta, dict):
            plan.rejects["no metadata"] += 1
            continue
        text = meta.get("AMAZON_BEDROCK_TEXT")
        if not isinstance(text, str) or not text.strip():
            plan.rejects["no text"] += 1
            continue
        try:
            bedrock = json.loads(meta.get("AMAZON_BEDROCK_METADATA") or "{}")
        except (TypeError, ValueError):
            plan.rejects["unreadable AMAZON_BEDROCK_METADATA"] += 1
            continue
        source = (bedrock.get("source") or {}).get("sourceLocation") or bedrock.get("sourceDocumentId")
        if not isinstance(source, str) or not source:
            plan.rejects["no source document id"] += 1
            continue
        namespace = _namespace(meta.get("namespace"))
        if namespace is None:
            plan.rejects["unknown namespace value"] += 1
            continue
        created = _parse_time(bedrock.get("createDate")) or _parse_time(bedrock.get("modifiedDate"))
        if created is None:
            plan.rejects["no createDate"] += 1
            continue
        by_doc[(namespace, source)].append((text.strip(), created))

    seen: set[tuple[str, str]] = set()
    for (namespace, source), chunks in sorted(by_doc.items()):
        plan.documents += 1
        if len(chunks) > 1:
            plan.multi_chunk_documents += 1
        doc_hash = _sha(source, 20)
        for text, created in sorted(chunks):
            if (namespace, text) in seen:
                plan.duplicates += 1
                continue
            seen.add((namespace, text))
            parts = _parts(text)
            plan.splits += len(parts) - 1
            for index, part in enumerate(parts):
                suffix = f"-p{index}" if len(parts) > 1 else ""
                request_id = f"kb-{doc_hash}-{_sha(text, 16)}{suffix}"
                plan.records.append(Record(request_id, namespace, part, created, doc_hash))
    return plan


# --- reporting ---------------------------------------------------------------------------------


def _mask(namespace: str, show: bool) -> str:
    if show or not namespace.startswith(f"/facts/{PERSONAL_PREFIX}"):
        return namespace
    uid = namespace[len(f"/facts/{PERSONAL_PREFIX}") : -1]
    return f"/facts/{PERSONAL_PREFIX}…{uid[-3:]}/"


def report(plan: Plan, *, show_namespaces: bool = False, out: Callable[[str], None] = print) -> None:
    per = plan.per_namespace()
    personal = {ns: n for ns, n in per.items() if ns != TEAM_NAMESPACE}
    out(f"vectors read:          {plan.vectors}")
    out(f"source documents:      {plan.documents} ({plan.multi_chunk_documents} with more than one chunk)")
    out(f"records to write:      {len(plan.records)}")
    out(f"  team ({TEAM_NAMESPACE}): {per.get(TEAM_NAMESPACE, 0)}")
    out(f"  personal:            {sum(personal.values())} across {len(personal)} namespaces")
    for ns, n in sorted(personal.items()):
        out(f"    {_mask(ns, show_namespaces)}: {n}")
    out(f"duplicates dropped:    {plan.duplicates}")
    out(f"splits (over {RECORD_MAX_CHARS} chars): {plan.splits}")
    out(f"rejects:               {sum(plan.rejects.values())}")
    for reason, n in sorted(plan.rejects.items()):
        out(f"    {reason}: {n}")
    out(f"BatchCreateMemoryRecords calls: {(len(plan.records) + BATCH_SIZE - 1) // BATCH_SIZE} (≤{BATCH_SIZE} each)")


# --- writing and verifying ---------------------------------------------------------------------


def _code(error: BaseException) -> str:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return str((response.get("Error") or {}).get("Code") or "")
    return ""


def call_with_backoff(
    fn: Callable[..., Any], *, attempts: int = 8, base: float = 0.5, sleep: Callable[[float], None] = time.sleep,
    **params: Any,
) -> Any:
    for attempt in range(attempts):
        try:
            return fn(**params)
        except Exception as e:  # noqa: BLE001 — only retryable service errors are retried
            if _code(e) not in _RETRYABLE or attempt == attempts - 1:
                raise
            sleep(min(base * 2**attempt, 20.0))
    raise AssertionError("unreachable")


def existing_migration_ids(
    client: Any, memory_id: str, namespace: str, *, sleep: Callable[[float], None] = time.sleep
) -> set[str]:
    """``migration_id`` of every migrated record already in ``namespace`` (``ListMemoryRecords``)."""
    found: set[str] = set()
    token: str | None = None
    while True:
        params: dict[str, Any] = {"memoryId": memory_id, "namespace": namespace, "maxResults": LIST_PAGE}
        if token:
            params["nextToken"] = token
        page = call_with_backoff(client.list_memory_records, sleep=sleep, **params)
        for summary in page.get("memoryRecordSummaries") or []:
            meta = summary.get("metadata") or {}
            if (meta.get("origin") or {}).get("stringValue") == ORIGIN:
                mid = (meta.get("migration_id") or {}).get("stringValue")
                if mid:
                    found.add(mid)
        token = page.get("nextToken")
        if not token:
            return found


@dataclass
class ApplyResult:
    written: int = 0
    skipped: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


def apply(
    plan: Plan,
    client: Any,
    memory_id: str,
    *,
    pause: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> ApplyResult:
    result = ApplyResult()
    by_namespace: dict[str, list[Record]] = defaultdict(list)
    for record in plan.records:
        by_namespace[record.namespace].append(record)
    for namespace, records in sorted(by_namespace.items()):
        done = existing_migration_ids(client, memory_id, namespace, sleep=sleep)
        todo = [r for r in records if r.request_id not in done]
        result.skipped += len(records) - len(todo)
        for start in range(0, len(todo), BATCH_SIZE):
            batch = todo[start : start + BATCH_SIZE]
            token = hashlib.sha256("|".join(r.request_id for r in batch).encode()).hexdigest()
            response = call_with_backoff(
                client.batch_create_memory_records,
                sleep=sleep,
                memoryId=memory_id,
                records=[r.request() for r in batch],
                clientToken=token,
            )
            result.written += len(response.get("successfulRecords") or [])
            for failure in response.get("failedRecords") or []:
                result.failed.append(
                    (str(failure.get("requestIdentifier")), str(failure.get("errorMessage") or failure.get("errorCode")))
                )
            if pause:
                sleep(pause)
        out(f"applied {namespace if namespace == TEAM_NAMESPACE else 'a personal namespace'}: "
            f"{len(todo)} sent, {len(records) - len(todo)} already present")
    return result


def verify(
    plan: Plan,
    client: Any,
    memory_id: str,
    *,
    show_namespaces: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> bool:
    ok = True
    for namespace, expected in sorted(plan.per_namespace().items()):
        present = existing_migration_ids(client, memory_id, namespace, sleep=sleep)
        wanted = {r.request_id for r in plan.records if r.namespace == namespace}
        missing = len(wanted - present)
        ok = ok and missing == 0
        out(f"verify {_mask(namespace, show_namespaces)}: expected {expected}, present {len(present & wanted)}, "
            f"missing {missing}")
    return ok


# --- CLI ---------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-file", type=Path, help="a dump of list-vectors --return-metadata output")
    src.add_argument("--vector-bucket", help="the S3 Vectors bucket behind the knowledge base")
    p.add_argument("--index", help="the S3 Vectors index (with --vector-bucket)")
    p.add_argument("--region")
    p.add_argument("--profile")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read only and report (the default)")
    mode.add_argument("--apply", action="store_true", help="write the records (needs --memory-id and --yes)")
    mode.add_argument("--verify", action="store_true", help="count migrated records per namespace")
    p.add_argument("--memory-id")
    p.add_argument("--yes", action="store_true", help="confirm --apply")
    p.add_argument("--show-namespaces", action="store_true", help="print personal namespaces unmasked")
    return p


def main(argv: list[str] | None = None, *, session: Any | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.vector_bucket and not args.index:
        print("--vector-bucket needs --index", file=sys.stderr)
        return 2
    if (args.apply or args.verify) and not args.memory_id:
        print("--apply/--verify need --memory-id", file=sys.stderr)
        return 2
    if args.apply and not args.yes:
        print("--apply writes to the Memory; add --yes to confirm", file=sys.stderr)
        return 2

    if session is None and (args.vector_bucket or args.apply or args.verify):
        import boto3

        session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if args.from_file:
        vectors: Iterable[dict[str, Any]] = vectors_from_file(args.from_file)
    else:
        vectors = vectors_from_index(session.client("s3vectors"), args.vector_bucket, args.index)
    plan = build_plan(vectors)
    report(plan, show_namespaces=args.show_namespaces)
    if not (args.apply or args.verify):
        print("dry run: nothing was written")
        return 0 if not plan.rejects else 1

    memory = session.client("bedrock-agentcore")
    if args.apply:
        result = apply(plan, memory, args.memory_id)
        print(f"written {result.written}, already present {result.skipped}, failed {len(result.failed)}")
        for request_id, reason in result.failed[:20]:
            print(f"  failed {request_id}: {reason}")
    ok = verify(plan, memory, args.memory_id, show_namespaces=args.show_namespaces)
    print("verified" if ok else "NOT complete: re-run --apply (it resumes)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
