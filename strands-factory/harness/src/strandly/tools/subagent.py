"""The ``subagent`` tool: the harness's ``make_subagent`` with a Strandly child builder.

The harness owns the schema, the child lifecycle, interrupt and cancel propagation and the depth
budget. Strandly builds each child through ``core.agent.assemble_agent`` at ``depth + 1`` with
read-only memory, the parent's sandbox and started MCP clients, and a tier from ``MODEL_TIERS``. A
skill's role files are harness presets (``agent_type``); a role's ``allowed-tools:`` frontmatter
scopes the child when ``tools`` is omitted. Calls are validated first: unknown tools or tiers are
errors, and so is role-file content pasted into ``instructions``.
The run's own cancel signal (:data:`RUN_CANCEL_SIGNAL_STATE_KEY`) outlives the parent invocation's,
so a queued call still sees a cancel and never builds a child.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

from strands.tools.tools import AgentTool
from strands.types._events import ToolInterruptEvent, ToolResultEvent, ToolStreamEvent

from strandly.core.constants import (
    MODEL_TIER_DEFAULT,
    MODEL_TIERS,
    SUBAGENT_MAX_DEPTH,
    SUBAGENT_SCOPABLE_TOOLS,
    SUBAGENT_UNGATED_TOOLS,
)
from strandly.core.context import RuntimeContext

if TYPE_CHECKING:
    from strands.types.tools import ToolGenerator, ToolSpec, ToolUse
    from strands_harness.tools.subagent import AgentSpec, Preset

    from strandly.core.config import Config

logger = logging.getLogger(__name__)

SUBAGENT_TOOL_NAME = "subagent"
# Role files under a skills root, each a preset named ``<skill>/<role>``.
ROLE_GLOB = "*/assets/roles/*.md"
# The preset a call without ``agent_type`` gets: no role text, the full tool set.
DEFAULT_ROLE = "general"
# Invocation-state key for the run's caller-owned cancel signal (set by ``serve.turn``).
RUN_CANCEL_SIGNAL_STATE_KEY = "strandly.run_cancel_signal"
# Trace attribute naming the session of the top-level run a child was delegated from.
PARENT_SESSION_TRACE_ATTRIBUTE = "strandly.parent_session.id"

# The children the current call's builder made, so the call can cancel them.
_CALL_CHILDREN: ContextVar[list[Any] | None] = ContextVar("subagent_call_children", default=None)
_CANCEL_POLL_SECONDS = 0.05

_TIER_DESCRIPTIONS: dict[str, str] = {
    "default": (
        "Your own model, the ordinary-work tier (used when omitted). Pick on depth, not size: "
        "'default' and 'advanced' share a 1M context window."
    ),
    "fast": "Haiku 5.5, cheap and quick: routing decisions, formatting, small summaries.",
    "advanced": (
        "Fable 5.2 at max effort: passes where the quality of thought is the deliverable "
        "(adversarial testing, API bar-raising, subtle-correctness hunts, gnarly debugging). "
        "Thinks longer and costs more every turn."
    ),
}

_GUIDANCE = (
    "\n\nThe subagent shares your sandbox (same working directory and files) but not this "
    "conversation: `task` must state the goal, the "
    "background, and where to look (paths, identifiers, commands). Every call runs in the "
    "background: you get a task id at once and the subagent's final report is delivered to you "
    "automatically when it finishes, so fan several out and keep working. You own synthesizing "
    "their results.\n"
    "`agent_type` runs a skill's role (`<skill>/<role>`, the file "
    "`skills/<skill>/assets/roles/<role>.md`): its instructions, and its tool scope when you omit "
    "`tools`. Pass the name, never the file's content. `instructions` is literal text for a role "
    "no skill defines; pass it or `agent_type`, not both.\n"
    "`tools` is a capability gate: the subagent is built with only the tools you list, so one "
    "without `write`/`edit` cannot modify files and one without `use_github` cannot reach the "
    "GitHub API (`shell`, if kept, is still a shell). Omit it to use the role's scope, or with no "
    "role scope, your full set. When you pass it, name at least one tool: there is no tool-less "
    "subagent, so an empty list is rejected.\n"
    "A subagent can fail transiently (an API error, a corrupted child state); the failure comes "
    "back as an error result. Retry once or twice: a retry is a fresh subagent. If it keeps "
    "failing, proceed without that pass and say which pass didn't complete and why. If an "
    "`advanced` subagent is the one failing, re-run that pass on the default tier instead."
)

_INSTRUCTIONS_DESCRIPTION = (
    "The subagent's role, as literal system-prompt text, for a role no skill defines. For a "
    "skill's role, pass `agent_type` instead."
)

_DEFAULT_ROLE_DESCRIPTION = "no role: your `instructions`, if any, and your full tool set (the default)"


# An ``allowed-tools:`` line anywhere: plain, ``read``-numbered, or inside JSON-escaped output.
_ALLOWED_TOOLS_LINE = re.compile(r"(?:^|\\n)\s*(?:\d+(?:\t|\\t))?allowed-tools\s*:", re.MULTILINE)
_ROLE_TITLE = re.compile(r"^#\s*Role:\s*(.+)$", re.MULTILINE)


def load_roles(skills_root: Path) -> dict[str, Preset]:
    """The role files under ``skills_root`` as harness presets named ``<skill>/<role>``.

    A role file is an optional ``---`` block holding one ``allowed-tools:`` line, then the role's
    instructions; its ``# Role:`` heading is the preset's description.

    Raises:
        ValueError: A role's frontmatter is unterminated, has other keys, or declares no tools.
    """
    from strands_harness.tools.subagent import Preset

    roles: dict[str, Preset] = {}
    for path in sorted(skills_root.glob(ROLE_GLOB)):
        name = f"{path.parts[-4]}/{path.stem}"
        body, tools = _parse_role(path.read_text(encoding="utf-8"), name)
        title = _ROLE_TITLE.search(body)
        roles[name] = Preset(
            instructions=body, tools=tools, description=title.group(1).strip() if title else name
        )
    return roles


def _parse_role(text: str, name: str) -> tuple[str, list[str] | None]:
    if not text.startswith("---\n"):
        if _ALLOWED_TOOLS_LINE.search(text):
            raise ValueError(f"role {name}: `allowed-tools:` outside a leading frontmatter block")
        return text.strip(), None
    head, sep, body = text[len("---\n") :].partition("\n---\n")
    if not sep:
        raise ValueError(f"role {name}: unterminated frontmatter")
    fields = dict(line.split(":", 1) for line in head.splitlines() if line.strip())
    scope = parse_tool_scope(fields.pop("allowed-tools", ""))
    if fields or not scope:
        raise ValueError(f"role {name}: frontmatter must be exactly one non-empty `allowed-tools:`")
    return body.strip(), sorted(scope)


def parse_tool_scope(raw: Any) -> frozenset[str]:
    """Tool names from a ``tools`` list or an ``allowed-tools:`` value (``shell read``,
    ``[shell, read]``, ``shell, read``)."""
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, str) else []
    names: set[str] = set()
    for item in items:
        cleaned = str(item).strip().strip("[]").replace(",", " ")
        names.update(n.strip("\"'") for n in cleaned.split() if n.strip("\"'"))
    return frozenset(names)


def _scope(requested: frozenset[str], source: str, offered: frozenset[str]) -> list[str]:
    """The names to pass to the harness for a requested scope; raises on an unusable scope."""
    unknown = requested - SUBAGENT_SCOPABLE_TOOLS - SUBAGENT_UNGATED_TOOLS
    if unknown:
        raise ValueError(
            f"unknown tool name(s) in {source}: {' '.join(sorted(unknown))}. Scopable tools are: "
            f"{' '.join(sorted(offered))}. Fix the names, or omit the scope to grant the full set."
        )
    gated = requested & SUBAGENT_SCOPABLE_TOOLS
    if not gated:
        raise ValueError(
            f"the scope in {source} names only tools that cannot be gated "
            f"({' '.join(sorted(requested))}), which would leave the subagent unrestricted. "
            f"Name at least one of: {' '.join(sorted(offered))}."
        )
    missing = gated - offered - {SUBAGENT_TOOL_NAME}
    if missing:
        raise ValueError(
            f"{source} names tool(s) you don't have, so the subagent can't get them: "
            f"{' '.join(sorted(missing))}. Drop them, or pick from: {' '.join(sorted(offered))}."
        )
    granted = sorted(gated & offered)
    if not granted:
        raise ValueError(
            f"the scope in {source} leaves the subagent no tools (subagents cannot delegate). "
            f"Name at least one of: {' '.join(sorted(offered))}."
        )
    return granted


def prepare_input(
    raw: dict[str, Any], offered: frozenset[str], roles: Mapping[str, Preset] | None = None
) -> dict[str, Any]:
    """Validate a call's input and rewrite it into what the harness tool should resolve.

    The model tier must be a ``MODEL_TIERS`` key, and ``instructions`` must not be a pasted role
    file. The scope (``tools``, else the ``agent_type`` role's ``allowed-tools:``) is checked
    against the offered names. Only an omitted scope removes ``tools``, which the builder reads as
    the full set; an empty scope is an error.
    """
    prepared = dict(raw)
    model = prepared.pop("model", None)
    if model:
        if model not in MODEL_TIERS:
            raise ValueError(
                f"unknown model tier {model!r}. Pass one of {', '.join(sorted(MODEL_TIERS))}, or "
                "omit `model` for the default; arbitrary model ids are not accepted."
            )
        prepared["model"] = model
    instructions = prepared.pop("instructions", None)
    if isinstance(instructions, str) and instructions.strip():
        if _ALLOWED_TOOLS_LINE.search(instructions):
            raise ValueError(
                "`instructions` holds a role file's content (it has an `allowed-tools:` line). "
                "Pass the role's name as `agent_type` (`<skill>/<role>`) instead."
            )
        if prepared.get("agent_type") not in (None, DEFAULT_ROLE):
            raise ValueError(
                "pass `agent_type` or `instructions`, not both: the role is its instructions. Put "
                "anything else the subagent needs in `task`."
            )
        prepared["instructions"] = instructions.strip()
    role = (roles or {}).get(str(prepared.get("agent_type")))
    tools = prepared.pop("tools", None)
    if tools is not None:
        source = "`tools`"
        requested = parse_tool_scope(tools)
    elif role is not None and role.tools is not None:
        source = f"the {prepared['agent_type']} role's `allowed-tools:`"
        requested = frozenset(role.tools)
    else:
        return prepared
    if not requested:
        raise ValueError(
            f"{source} is empty, and a subagent can't have no tools. Name at least one tool it "
            "may use, or omit `tools` for the role's scope (or, with no role, the full set)."
        )
    prepared["tools"] = _scope(requested, source, offered)
    return prepared


class _StrandlySubagentTool(AgentTool):
    """The harness tool behind input validation, with child stream events dropped."""

    def __init__(self, inner: AgentTool, offered: frozenset[str], roles: Mapping[str, Preset]) -> None:
        super().__init__()
        self._inner = inner
        self._offered = offered
        self._roles = roles

    @property
    def tool_name(self) -> str:
        return self._inner.tool_name

    @property
    def tool_spec(self) -> ToolSpec:
        return self._inner.tool_spec

    @property
    def tool_type(self) -> str:
        return self._inner.tool_type

    async def stream(
        self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any
    ) -> ToolGenerator:
        tool_use_id = tool_use["toolUseId"]
        run_signal = invocation_state.get(RUN_CANCEL_SIGNAL_STATE_KEY)
        if not isinstance(run_signal, threading.Event):
            run_signal = None
        try:
            if run_signal is not None and run_signal.is_set():
                raise ValueError("the run was cancelled before this subagent started.")
            prepared = prepare_input(dict(tool_use.get("input") or {}), self._offered, self._roles)
        except ValueError as exc:
            logger.info("subagent not started: tool_use_id=%s: %s", tool_use_id, exc)
            yield _error(tool_use_id, str(exc))
            return
        call: Any = {**tool_use, "input": prepared}
        result: ToolResultEvent | ToolInterruptEvent | None = None
        # The harness resumes an interrupted child without invoking our builder again.
        pending = getattr(self._inner, "_pending", {}).get(tool_use_id)
        children: list[Any] = [pending] if pending is not None else []
        token = _CALL_CHILDREN.set(children)
        watcher = asyncio.create_task(
            _cancel_with_parent(invocation_state.get("agent"), children, run_signal)
        )
        try:
            # The child shares the parent's sandbox but can outlive the parent's turn, which
            # unbinds the sandbox's cancel signal; its commands follow the run's signal instead.
            with _child_command_cancel(run_signal):
                async with contextlib.aclosing(
                    self._inner.stream(call, invocation_state, **kwargs)
                ) as events:
                    async for event in events:
                        if isinstance(event, ToolStreamEvent):
                            continue
                        # Both terminal event types leave the child context before reaching
                        # the caller, which may close this generator from a different task.
                        result = event
                        break
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            _CALL_CHILDREN.reset(token)
        status = "no result"
        if isinstance(result, ToolResultEvent):
            status = str(result.tool_result.get("status"))
        elif isinstance(result, ToolInterruptEvent):
            status = "interrupted"
        logger.info("subagent finished: tool_use_id=%s status=%s", tool_use_id, status)
        if result is not None:
            yield result


def _child_command_cancel(run_signal: threading.Event | None) -> contextlib.AbstractContextManager[None]:
    """Make the child's sandbox commands stop waiting once ``run_signal`` is set."""
    if run_signal is None:
        return contextlib.nullcontext()
    from strandly.sandbox.runtime import command_cancel_signal

    return command_cancel_signal(run_signal)


def _error(tool_use_id: str, message: str) -> ToolResultEvent:
    return ToolResultEvent(
        {
            "toolUseId": tool_use_id,
            "status": "error",
            "content": [{"text": f"Subagent error: {message}"}],
        }
    )


async def _cancel_with_parent(
    parent: Any, children: list[Any], run_signal: threading.Event | None = None
) -> None:
    """Cancel this call's children once the parent's run is cancelled.

    The harness links a child to the parent's own signal, which the SDK clears when the parent's
    invocation ends, so a cancelled run can end before the child polls it. The run's caller-owned
    signals (``run_signal`` and the parent's external one, captured now) stay set, so they are
    watched too.
    """
    signals = [
        signal
        for signal in (
            getattr(parent, "cancel_signal", None),
            getattr(parent, "_external_cancel_signal", None),
            run_signal,
        )
        if isinstance(signal, threading.Event)
    ]
    if not signals:
        return
    while not any(signal.is_set() for signal in signals):
        await asyncio.sleep(_CANCEL_POLL_SECONDS)
    for child in children:
        child.cancel()


def offered_tools(allowed_tools: frozenset[str] | None) -> frozenset[str]:
    """The names a child may be scoped to: the scopable set within ``allowed_tools``, minus
    ``subagent`` (children cannot delegate)."""
    return frozenset(
        name
        for name in SUBAGENT_SCOPABLE_TOOLS - {SUBAGENT_TOOL_NAME}
        if allowed_tools is None or name in allowed_tools
    )


def make_builder(
    config: Config,
    ctx: RuntimeContext,
    sandbox: Any,
    *,
    depth: int = 0,
    allowed_tools: frozenset[str] | None = None,
    mcp_clients: list[Any] | None = None,
) -> Callable[[AgentSpec], Any]:
    """The harness ``AgentBuilder``: a child at ``depth + 1`` sharing ``sandbox`` and
    ``mcp_clients``, isolated from the parent's session."""
    offered = offered_tools(allowed_tools)

    def builder(spec: AgentSpec) -> Any:
        from strandly.core.agent import assemble_agent

        tier = spec.model if isinstance(spec.model, str) and spec.model else MODEL_TIER_DEFAULT
        model, effort = MODEL_TIERS[tier]
        tools = spec.tools
        if tools is None or (allowed_tools is None and offered <= set(tools)):
            scope = None
        else:
            scope = frozenset(tools)
        child_depth = depth + 1
        logger.info(
            "subagent start: depth=%d tier=%s scope=%s",
            child_depth,
            tier,
            "all" if scope is None else ",".join(sorted(scope)) or "-",
        )
        child = assemble_agent(
            config,
            RuntimeContext(
                cwd=ctx.cwd,
                session_id=None,
                session_key=None,
                event=ctx.event,
                metadata=dict(ctx.metadata),
                now=ctx.now,
                parent_session_id=ctx.parent_session_id or ctx.session_id,
                model=ctx.model,
                effort=ctx.effort,
            ),
            system_prompt=spec.instructions or "",
            model=model,
            effort=effort,
            sandbox=sandbox,
            spawn_depth=child_depth,
            allowed_tools=scope,
            mcp_clients=mcp_clients,
        )
        logger.info("subagent built: depth=%d tools=%d", child_depth, len(child.tool_names))
        call_children = _CALL_CHILDREN.get()
        if call_children is not None:
            call_children.append(child)
        return child

    return builder


def make_subagent_tool(
    config: Config,
    ctx: RuntimeContext,
    sandbox: Any,
    *,
    depth: int = 0,
    allowed_tools: frozenset[str] | None = None,
    mcp_clients: list[Any] | None = None,
    roles: Mapping[str, Preset] | None = None,
) -> AgentTool:
    """The ``subagent`` tool for an agent at ``depth``; children share ``sandbox`` and
    ``mcp_clients`` and never get more than ``allowed_tools`` (``None`` = unscoped). ``roles``
    (see :func:`load_roles`) become its ``agent_type`` choices."""
    from strands_harness.tools.subagent import (
        Choice,
        Fixed,
        Open,
        Option,
        Preset,
        make_subagent,
    )

    offered = offered_tools(allowed_tools)
    presets = {DEFAULT_ROLE: Preset(description=_DEFAULT_ROLE_DESCRIPTION), **(roles or {})}
    inner = make_subagent(
        name=SUBAGENT_TOOL_NAME,
        builder=make_builder(
            config,
            ctx,
            sandbox,
            depth=depth,
            allowed_tools=allowed_tools,
            mcp_clients=mcp_clients,
        ),
        instructions=Open(),
        tools=Choice(sorted(offered), multiple=True) if offered else Fixed([]),
        model=Choice([Option(name, description=desc) for name, desc in _TIER_DESCRIPTIONS.items()]),
        context=Fixed("none"),
        presets=presets,
        default_preset=DEFAULT_ROLE,
        max_depth=SUBAGENT_MAX_DEPTH,
    )
    spec = inner.tool_spec
    spec["description"] += _GUIDANCE
    properties = spec["inputSchema"]["json"]["properties"]
    properties["instructions"]["description"] = _INSTRUCTIONS_DESCRIPTION
    if properties.get("tools", {}).get("type") == "array":
        properties["tools"]["minItems"] = 1
    return _StrandlySubagentTool(inner, offered, presets)


__all__ = [
    "DEFAULT_ROLE",
    "PARENT_SESSION_TRACE_ATTRIBUTE",
    "ROLE_GLOB",
    "RUN_CANCEL_SIGNAL_STATE_KEY",
    "SUBAGENT_TOOL_NAME",
    "load_roles",
    "make_builder",
    "make_subagent_tool",
    "offered_tools",
    "parse_tool_scope",
    "prepare_input",
]
