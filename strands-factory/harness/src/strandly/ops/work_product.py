"""Work-product store: the structured review, triage and first-response artifacts, in DynamoDB.

GitHub gets a distilled summary; the full artifact (findings, confidence, suppressed findings with
reasons, evidence) lives here for the portal to list, curate and post. Enabled by
``STRANDLY_WORK_PRODUCT_TABLE``. Writes are fail-open; reads raise, so the read API decides how to
show a storage failure instead of an empty inbox. Two newest-first GSIs, ``by_status`` (the inbox)
and ``by_session`` (one item's history); their names are mirrored in infra and the dashboard API.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from strandly.core.config import Config
from strandly.core.constants import (
    WORK_PRODUCT_SESSION_GSI_NAME,
    WORK_PRODUCT_STATUS_GSI_NAME,
)

logger = logging.getLogger(__name__)

# The three lifecycle states an artifact moves through. ``draft`` is what the
# agent emits working ahead; ``curated`` once a human has selected which findings post; ``posted``
# once the distilled result reached GitHub.
STATUS_DRAFT = "draft"
STATUS_CURATED = "curated"
STATUS_POSTED = "posted"

# The complete set of valid lifecycle states. A curation write MUST land in one of these — an
# arbitrary status (e.g. "banana") would make the row vanish from every ``by_status`` view (the GSI
# keys on status), silently losing the artifact.
VALID_STATUSES = frozenset({STATUS_DRAFT, STATUS_CURATED, STATUS_POSTED})

# The artifact kinds — one per skill that emits.
KIND_REVIEW = "review"
KIND_TRIAGE = "triage"
KIND_FIRST_RESPONSE = "first-response"

# Keep stored free-text bounded — this is an index/curation record, not a transcript store.
_TEXT_LIMIT = 8_000
_COMMENT_LIMIT = 8_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Finding:
    """One reviewer finding — a single item the human toggles in/out during curation.

    ``tier`` is the code-review severity (``block``/``should-fix``/``nit`` — the 🔴/🟡/⚪ tiers).
    ``suppressed`` marks a finding the aggregator chose NOT to post (with ``suppress_reason``); the
    portal shows these collapsed so a human can rescue a false-suppress. ``included`` is the
    curation decision — whether this finding is in the set that will post (defaults to True for a
    non-suppressed finding, False for a suppressed one).
    """

    id: str = ""
    file: str = ""
    line: int | None = None
    tier: str = ""
    category: str = ""
    confidence: float | None = None
    text: str = ""
    suggestion: str = ""
    reachability: str = ""
    suppressed: bool = False
    suppress_reason: str = ""
    included: bool | None = None

    def to_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {"id": self.id or uuid.uuid4().hex[:12]}
        _set_truthy(item, file=self.file, tier=self.tier, category=self.category,
                    suggestion=self.suggestion[:_TEXT_LIMIT] if self.suggestion else "",
                    reachability=self.reachability, suppress_reason=self.suppress_reason)
        if self.text:
            item["text"] = self.text[:_TEXT_LIMIT]
        if self.line is not None:
            item["line"] = int(self.line)
        if self.confidence is not None:
            item["confidence"] = Decimal(str(self.confidence))
        item["suppressed"] = bool(self.suppressed)
        included = self.included if self.included is not None else (not self.suppressed)
        item["included"] = bool(included)
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Finding:
        conf = item.get("confidence")
        return cls(
            id=str(item.get("id", "")),
            file=str(item.get("file", "")),
            line=_coerce_line(item.get("line")),
            tier=str(item.get("tier", "")),
            category=str(item.get("category", "")),
            confidence=_coerce_confidence(conf),
            text=str(item.get("text", "")),
            suggestion=str(item.get("suggestion", "")),
            reachability=str(item.get("reachability", "")),
            suppressed=bool(item.get("suppressed", False)),
            suppress_reason=str(item.get("suppress_reason", "")),
            included=bool(item["included"]) if item.get("included") is not None else None,
        )


@dataclass
class WorkProduct:
    """A single review/triage/first-response artifact.

    ``session_id`` (``gh-<owner>-<repo>-<h8>-<kind>-<number>``) is the join key to the run-ledger and to
    every other artifact for the same GitHub item. ``run_task_id`` links back to the ledger row of
    the run that produced this artifact.
    """

    session_id: str
    kind: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = STATUS_DRAFT
    verdict: str = ""
    title: str = ""
    target_url: str = ""
    repo: str = ""
    number: int | None = None
    tldr: str = ""
    findings: list[Finding] = field(default_factory=list)
    evidence_ledger: dict[str, Any] = field(default_factory=dict)
    recommended_labels: list[str] = field(default_factory=list)
    draft_comment: str = ""
    priority: str = ""
    repro_status: str = ""
    suspected_area: str = ""
    duplicates: list[str] = field(default_factory=list)
    run_task_id: str = ""
    curated_by: str = ""
    posted_at: str = ""
    github_review_id: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_item(self) -> dict[str, Any]:
        """The DynamoDB item — required keys always present, optionals only when truthy."""
        item: dict[str, Any] = {
            "work_product_id": self.id,
            "session_id": self.session_id,
            "kind": self.kind,
            "status": self.status or STATUS_DRAFT,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        _set_truthy(
            item,
            verdict=self.verdict, title=self.title[:_TEXT_LIMIT] if self.title else "",
            target_url=self.target_url, repo=self.repo,
            priority=self.priority, repro_status=self.repro_status,
            suspected_area=self.suspected_area,
            run_task_id=self.run_task_id, curated_by=self.curated_by,
            posted_at=self.posted_at, github_review_id=self.github_review_id,
        )
        if self.number is not None:
            item["number"] = int(self.number)
        if self.tldr:
            item["tldr"] = self.tldr[:_TEXT_LIMIT]
        if self.draft_comment:
            item["draft_comment"] = self.draft_comment[:_COMMENT_LIMIT]
        if self.findings:
            item["findings"] = [f.to_item() for f in self.findings]
        if self.evidence_ledger:
            item["evidence_ledger"] = _floats_to_decimal(self.evidence_ledger)
        if self.recommended_labels:
            item["recommended_labels"] = list(self.recommended_labels)
        if self.duplicates:
            item["duplicates"] = list(self.duplicates)
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> WorkProduct:
        return cls(
            session_id=str(item.get("session_id", "")),
            kind=str(item.get("kind", "")),
            id=str(item.get("work_product_id", "")),
            status=str(item.get("status", STATUS_DRAFT)),
            verdict=str(item.get("verdict", "")),
            title=str(item.get("title", "")),
            target_url=str(item.get("target_url", "")),
            repo=str(item.get("repo", "")),
            number=int(item["number"]) if item.get("number") is not None else None,
            tldr=str(item.get("tldr", "")),
            findings=[Finding.from_item(f) for f in item.get("findings", [])],
            evidence_ledger=_decimal_to_floats(item.get("evidence_ledger", {})) or {},
            recommended_labels=list(item.get("recommended_labels", [])),
            draft_comment=str(item.get("draft_comment", "")),
            priority=str(item.get("priority", "")),
            repro_status=str(item.get("repro_status", "")),
            suspected_area=str(item.get("suspected_area", "")),
            duplicates=list(item.get("duplicates", [])),
            run_task_id=str(item.get("run_task_id", "")),
            curated_by=str(item.get("curated_by", "")),
            posted_at=str(item.get("posted_at", "")),
            github_review_id=str(item.get("github_review_id", "")),
            created_at=str(item.get("created_at", "")),
            updated_at=str(item.get("updated_at", "")),
        )


class WorkProductStore:
    """DynamoDB store for work products. Build via :meth:`from_config`; ``None`` when disabled.

    Writes (:meth:`put`, :meth:`mark_curated`) are fail-open — they log and swallow so a telemetry
    write never disrupts a run. Reads (:meth:`get`, :meth:`query_by_session`, :meth:`query_drafts`)
    let their errors propagate so the portal's read API can surface a storage failure rather than
    show an empty inbox. The boto3 Table is built lazily on first use (or injected for tests).
    """

    def __init__(self, table_name: str, region: str | None = None, *, table: Any | None = None):
        self.table_name = table_name
        self.region = region
        self._table = table

    @classmethod
    def from_config(cls, config: Config) -> WorkProductStore | None:
        """Return a store if ``STRANDLY_WORK_PRODUCT_TABLE`` is set, else ``None`` (disabled)."""
        if not config.work_product_enabled:
            return None
        return cls(table_name=config.work_product_table or "", region=config.aws_region)

    # ---- public API ----------------------------------------------------------------

    def put(self, wp: WorkProduct) -> None:
        """Upsert one work product; fail-open (log + swallow)."""
        wp.updated_at = _now_iso()
        try:
            self.table().put_item(Item=wp.to_item())
            logger.info("work-product put id=%s kind=%s status=%s session=%s",
                        wp.id, wp.kind, wp.status, wp.session_id)
        except Exception:  # noqa: BLE001 - fail-open by design
            logger.warning("work-product write failed (id=%s session=%s); continuing",
                           wp.id, wp.session_id, exc_info=True)

    def get(self, work_product_id: str) -> WorkProduct | None:
        """One artifact by id, or ``None`` if absent. Read errors propagate."""
        resp = self.table().get_item(Key={"work_product_id": work_product_id})
        item = resp.get("Item")
        return WorkProduct.from_item(item) if item else None

    def query_by_session(self, session_id: str, *, limit: int = 100) -> list[WorkProduct]:
        """Every artifact for one GitHub item, newest-first (``by_session`` GSI)."""
        from boto3.dynamodb.conditions import Key

        resp = self.table().query(
            IndexName=WORK_PRODUCT_SESSION_GSI_NAME,
            KeyConditionExpression=Key("session_id").eq(session_id),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [WorkProduct.from_item(i) for i in resp.get("Items", [])]

    def query_drafts(self, *, kind: str | None = None, limit: int = 100) -> list[WorkProduct]:
        """Draft artifacts newest-first (``by_status`` GSI) — the portal "Today" inbox.

        ``kind`` optionally filters to one artifact type (review/triage/first-response); the filter
        is applied client-side on the (small) draft page rather than as a DynamoDB FilterExpression,
        so ``limit`` bounds the rows read regardless of the kind mix.
        """
        from boto3.dynamodb.conditions import Key

        resp = self.table().query(
            IndexName=WORK_PRODUCT_STATUS_GSI_NAME,
            KeyConditionExpression=Key("status").eq(STATUS_DRAFT),
            ScanIndexForward=False,
            Limit=limit,
        )
        products = [WorkProduct.from_item(i) for i in resp.get("Items", [])]
        if kind:
            products = [p for p in products if p.kind == kind]
        return products

    def mark_curated(
        self,
        work_product_id: str,
        *,
        included_finding_ids: list[str] | None = None,
        curated_by: str = "",
        status: str = STATUS_CURATED,
        tldr: str | None = None,
        verdict: str | None = None,
        finding_text_overrides: dict[str, str] | None = None,
    ) -> WorkProduct | None:
        """Persist a curation decision and move the artifact to ``status`` (default ``curated``).

        The Review console lets a human edit before publishing, so curation carries more than a
        selection:

        * ``included_finding_ids`` — set each finding's ``included`` flag (None → leave untouched,
          e.g. a verdict-only curation).
        * ``finding_text_overrides`` — ``{finding_id: edited_text}``; replaces the finding body a
          human sharpened.
        * ``tldr`` / ``verdict`` — the edited headline / verdict (None → keep the drafted value).

        Fail-open; returns the updated product, or ``None`` if it doesn't exist / the read failed.

        Raises ``ValueError`` for a ``status`` outside :data:`VALID_STATUSES` — an arbitrary state
        would drop the row from every ``by_status`` view (silent data loss), so the store rejects it
        rather than persisting it.
        """
        if status not in VALID_STATUSES:
            raise ValueError(
                f"invalid status {status!r}; must be one of {sorted(VALID_STATUSES)}"
            )
        try:
            wp = self.get(work_product_id)
        except Exception:  # noqa: BLE001 - fail-open by design
            logger.warning("work-product curate read failed (id=%s)", work_product_id,
                           exc_info=True)
            return None
        if wp is None:
            return None
        if included_finding_ids is not None:
            chosen = set(included_finding_ids)
            for finding in wp.findings:
                finding.included = finding.id in chosen
        if finding_text_overrides:
            for finding in wp.findings:
                if finding.id in finding_text_overrides:
                    finding.text = finding_text_overrides[finding.id]
        if tldr is not None:
            wp.tldr = tldr
        if verdict is not None:
            wp.verdict = verdict
        wp.status = status
        if curated_by:
            wp.curated_by = curated_by
        self.put(wp)
        return wp

    # ---- internals -----------------------------------------------------------------

    def table(self) -> Any:
        """The boto3 DynamoDB ``Table`` resource, built lazily (cached)."""
        if self._table is None:
            import boto3

            session = boto3.Session(region_name=self.region) if self.region else boto3.Session()
            self._table = session.resource("dynamodb").Table(self.table_name)
        return self._table


def _set_truthy(item: dict[str, Any], **fields: str) -> None:
    """Set only the keys whose value is a non-empty string (DynamoDB rejects empty strings)."""
    for key, value in fields.items():
        if value:
            item[key] = value


# Models sometimes emit a word instead of the documented 0–1 number (prod 2026-09-25:
# ``"confidence": "high"`` raised ``ValueError`` and failed an otherwise-completed run).
_CONFIDENCE_WORDS = {
    "very high": 0.95,
    "high": 0.9,
    "medium": 0.6,
    "med": 0.6,
    "moderate": 0.6,
    "low": 0.3,
    "very low": 0.1,
}


def _coerce_confidence(value: Any) -> float | None:
    """Best-effort ``confidence`` → float in [0, 1], or ``None`` when it can't be read.

    Accepts numbers (``Decimal`` from DynamoDB included), numeric strings, percentages
    (``"85%"`` / ``85``), and the words high/medium/low. Never raises: this field comes from model
    output, and a bad value must not cost the whole work product.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        if text in _CONFIDENCE_WORDS:
            return _CONFIDENCE_WORDS[text]
        pct = text.endswith("%")
        try:
            number = float(text.rstrip("%").strip())
        except ValueError:
            logger.warning("unparseable finding confidence %r; storing none", value)
            return None
        if pct:
            number /= 100
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            logger.warning("unparseable finding confidence %r; storing none", value)
            return None
    if number != number:  # NaN
        return None
    if 1 < number <= 100:  # a percentage written without the sign
        number /= 100
    return min(max(number, 0.0), 1.0)


def _coerce_line(value: Any) -> int | None:
    """Best-effort ``line`` → int (``"88"``, ``"88-90"``, ``"L88"`` → 88), or ``None``. Never raises."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except OverflowError:  # an infinite float (JSON 1e999)
        return None
    except (TypeError, ValueError):
        pass
    digits = ""
    for ch in str(value).lstrip("Ll#: "):
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else None


def _floats_to_decimal(obj: Any) -> Any:
    """Recursively convert floats to ``Decimal`` — the DynamoDB resource rejects native floats."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimal(v) for v in obj]
    return obj


def _decimal_to_floats(obj: Any) -> Any:
    """Recursively convert ``Decimal`` back to int/float for JSON-friendly reads."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_floats(v) for v in obj]
    return obj
