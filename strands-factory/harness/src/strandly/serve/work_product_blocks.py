"""Parse and strip skill-emitted work-product blocks without storage or run lifecycle effects."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# The skill-emitted structured block the runtime serializes into a WorkProduct: a
# fenced code block whose info string is ``work-product``, holding one JSON object.
#
# The opening fence must start a line (indentation allowed, ``work_product`` and a trailing info
# word tolerated), so a mention of the fence inside a sentence or a ``>`` quote is prose, not a
# block. A closing fence is any line that ENDS with three backticks, so a body line followed by
# the fence on the same line closes the block too. A double-fenced body (an inner fence with a
# language, e.g. json or yaml, as its first line) consumes its inner and outer closing fences.
# A block left open runs to the end of the message (see :func:`_blocks_to_use` for when it counts).
_WP_OPEN_FENCE = re.compile(r"[ \t]*```[ \t]*work[-_]product\b[^\n]*")
_WP_INNER_FENCE = re.compile(r"[ \t]*```[ \t]*[A-Za-z][\w+-]*[ \t]*")
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
# ``https://<host>/<owner>/<repo>/(pull|issues|discussions)/<n>`` — a run's target and a block's.
_GITHUB_ITEM_URL = re.compile(
    r"https?://([^/\s]+)/([^/\s]+/[^/\s]+)/(?:pull|issues|discussions)/(\d+)(?=[/?#]|$)",
    re.IGNORECASE,
)
_PREVIEW_CHARS = 200
# Hostile-input bounds for the fallbacks. Real artifacts nest 3-4 levels and run to a few KB; the
# pure-Python YAML scanner is quadratic in flow nesting (3000 levels: seconds of CPU), so deeper or
# bigger bodies are refused before any parser sees them.
_MAX_NESTING = 64
_MAX_YAML_CHARS = 64 * 1024


@dataclass(frozen=True)
class _WorkProductBlock:
    start: int  # offset of the opening fence line
    end: int  # offset just past the closing fence line (or len(text) when left open)
    body: str
    closed: bool


def _find_work_product_blocks(text: str) -> list[_WorkProductBlock]:
    """Every work-product fence in ``text``, in order. Linear; never raises on any ``str``."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    blocks: list[_WorkProductBlock] = []
    i = 0
    while i < len(lines):
        if not _WP_OPEN_FENCE.fullmatch(lines[i].rstrip("\r\n")):
            i += 1
            continue
        j = i + 1
        nested = j < len(lines) and bool(_WP_INNER_FENCE.fullmatch(lines[j].rstrip("\r\n")))
        if nested:
            j += 1
        body: list[str] = []
        closed = False
        while j < len(lines):
            line = lines[j].rstrip("\r\n").rstrip()
            j += 1
            if line.endswith("```"):
                body.append(line[:-3])
                closed = True
                break
            body.append(line)
        if closed and nested:
            k = j
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k < len(lines) and lines[k].strip() == "```":
                j = k + 1
        blocks.append(_WorkProductBlock(offsets[i], offsets[j], "\n".join(body), closed))
        i = j
    return blocks


def _blocks_to_use(blocks: list[_WorkProductBlock]) -> list[_WorkProductBlock]:
    """The blocks that count: every closed one, or — only when there is none — the open one.

    An unclosed fence after a closed block is quoted text (a PR body, a prose mention); treating it
    as a block would let it outrank the real artifact and strip the rest of the message.
    """
    closed = [b for b in blocks if b.closed]
    return closed or blocks[-1:]


def _strip_blocks(text: str, blocks: list[_WorkProductBlock]) -> str:
    """``text`` with ``blocks`` cut out; the prose on either side stays a paragraph apart."""
    pieces: list[str] = []
    pos = 0
    for b in blocks:
        pieces.append(text[pos : b.start])
        pos = b.end
    pieces.append(text[pos:])
    out = ""
    for piece in pieces:
        if not piece.strip():
            continue
        piece = piece.lstrip("\n")
        out = out.rstrip("\n") + "\n\n" + piece if out else piece
    return out.strip()


def _parse_work_product_body(body: str) -> tuple[dict[str, Any] | None, str]:
    """Best-effort parse of a work-product block body → ``(dict | None, how-or-why)``.

    Strict JSON first, then progressively looser fallbacks: the outermost ``{...}`` when
    prose surrounds it, trailing commas removed, and finally YAML (the model sometimes writes
    ``kind: review`` / ``findings:`` lists) without anchors or aliases. A fallback result only
    counts if it is a mapping that names a ``kind`` or ``verdict`` — so arbitrary prose that
    happens to be valid YAML is not mistaken for an artifact. The second element is the parse
    route on success, else the reason. Bodies nested deeper than ``_MAX_NESTING`` are refused, and
    YAML is tried only up to ``_MAX_YAML_CHARS``. May still raise on hostile input (a YAML date key
    JSON cannot serialise); :func:`_split_work_product` catches.
    """
    text = body.strip()
    if _nesting_depth(text) > _MAX_NESTING:
        return None, f"nested deeper than {_MAX_NESTING} levels"
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    else:
        if isinstance(data, dict):
            return data, "json"
        return None, f"not a JSON object ({type(data).__name__})"

    candidates: list[tuple[str, str]] = []
    first, last = text.find("{"), text.rfind("}")
    if 0 <= first < last:
        candidates.append(("json-extracted", text[first : last + 1]))
    candidates.append(("json-repaired", _TRAILING_COMMA.sub(r"\1", text)))
    if candidates[0][0] == "json-extracted":
        candidates.append(("json-extracted-repaired", _TRAILING_COMMA.sub(r"\1", candidates[0][1])))
    for how, candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if _looks_like_work_product(data):
            return data, how

    if len(text) > _MAX_YAML_CHARS:
        return None, f"invalid JSON, and too large for the YAML fallback ({len(text)} chars)"
    try:
        import yaml  # transitive runtime dep (strands-agents); only needed on this fallback path
    except ImportError:
        return None, "invalid JSON (no YAML parser available)"
    try:
        # An alias can make the tree circular or expand it exponentially when serialised; an
        # artifact never needs one, so refuse them before building anything.
        if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)):
            return None, "YAML anchors/aliases are not accepted"
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None, "neither valid JSON nor YAML"
    if not _looks_like_work_product(data):
        return None, f"no JSON/YAML object with kind/verdict ({type(data).__name__})"
    # YAML yields dates/datetimes for bare ISO values — round-trip to plain JSON types so the
    # artifact stays serializable exactly like a JSON-parsed one.
    return json.loads(json.dumps(data, default=str)), "yaml"


def _nesting_depth(text: str) -> int:
    """Deepest ``[``/``{`` nesting in ``text``, brackets inside strings included (an upper bound)."""
    depth = deepest = 0
    for ch in text:
        if ch in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif ch in "]}" and depth:
            depth -= 1
    return deepest


def _looks_like_work_product(data: Any) -> bool:
    return isinstance(data, dict) and ("kind" in data or "verdict" in data)


def _github_item(url: Any) -> tuple[str, str, int] | None:
    """``(host, owner/repo, number)`` of a GitHub issue/PR/discussion URL, lower-cased; else None."""
    match = _GITHUB_ITEM_URL.match(url) if isinstance(url, str) else None
    if not match:
        return None
    return match.group(1).lower(), match.group(2).lower(), int(match.group(3))


def _target_mismatch(
    data: dict[str, Any], *, repo: str | None, target_url: str | None
) -> str | None:
    """Why a block is not about the run's own GitHub item, or ``None`` if nothing contradicts it.

    Every skill that emits a block acts on the item that triggered the run, so a block naming
    another repository, number or host is quoted or injected text, never this run's artifact.
    Fields the block omits are not a mismatch; a run with no GitHub target checks nothing.
    """
    run = _github_item(target_url)
    run_host = run[0] if run else None
    run_repo = repo.lower() if isinstance(repo, str) and repo else (run[1] if run else None)
    run_number = run[2] if run else None
    if run_repo is None and run_number is None:
        return None
    claimed_url = data.get("target_url")
    if claimed_url:
        claimed = _github_item(claimed_url)
        if claimed is None:
            return f"target_url {str(claimed_url)[:_PREVIEW_CHARS]!r} is not a GitHub item URL"
        url_host, url_repo, url_number = claimed
        if run_host and url_host != run_host:
            return f"target_url host {url_host!r} is not the run's {run_host!r}"
        if run_repo and url_repo != run_repo:
            return f"target_url repo {url_repo!r} is not the run's {run_repo!r}"
        if run_number is not None and url_number != run_number:
            return f"target_url number {url_number} is not the run's {run_number}"
    claimed_repo = data.get("repo")
    if claimed_repo and run_repo and str(claimed_repo).strip().lower() != run_repo:
        return f"repo {str(claimed_repo)[:_PREVIEW_CHARS]!r} is not the run's {run_repo!r}"
    claimed_number = _coerce_int(data.get("number"))
    if claimed_number is not None and run_number is not None and claimed_number != run_number:
        return f"number {claimed_number} is not the run's {run_number}"
    return None


def _split_work_product(
    text: str, *, repo: str | None = None, target_url: str | None = None
) -> tuple[str, dict[str, Any] | None, str, list[str]]:
    """``(text without its blocks, artifact | None, its parse route, why other blocks were not used)``.

    The artifact is the LAST counted block (see :func:`_blocks_to_use`) that parses and does not
    contradict the run's GitHub target (:func:`_target_mismatch`). Total: logs and counts nothing
    and never raises — a parser exception (a YAML date key JSON cannot serialise, a
    ``RecursionError`` on deep nesting, ...) is a reason, because the caller runs between "the agent
    finished" and ``store.finish``, where a raise would flip a completed run to failed.
    """
    blocks = _find_work_product_blocks(text)
    if not blocks:
        return text, None, "", []
    used = _blocks_to_use(blocks)
    stripped = _strip_blocks(text, used)
    reasons: list[str] = []
    for block in reversed(used):
        try:
            data, how = _parse_work_product_body(block.body)
            mismatch = None if data is None else _target_mismatch(
                data, repo=repo, target_url=target_url
            )
        except Exception as e:  # noqa: BLE001 — total by contract; the caller logs the reasons
            reasons.append(f"parser raised {type(e).__name__}: {str(e)[:_PREVIEW_CHARS]}")
            continue
        if data is None:
            reasons.append(f"{how} (body[:{_PREVIEW_CHARS}]={block.body[:_PREVIEW_CHARS]!r})")
        elif mismatch is not None:
            reasons.append(f"not this run's item: {mismatch}")
        else:
            return stripped, data, how, reasons
    return stripped, None, "", reasons


def _strip_work_product(text: str) -> str:
    """``text`` without its work-product blocks; no parsing, logging or counting."""
    blocks = _find_work_product_blocks(text)
    return _strip_blocks(text, _blocks_to_use(blocks)) if blocks else text


def _coerce_int(value: Any) -> int | None:
    """A block's ``number`` as an int, or ``None`` — never let a bad value drop the whole artifact."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):  # OverflowError: JSON 1e999 parses to inf
        return None
