"""System-prompt composition.

The prompt is the **global prompt** (``global_prompt.md`` — Strandly's identity, house rules, file
method, safety) plus a dynamically-built **capabilities** section describing only the tools the
agent *actually has* this turn, plus an optional ``system_prompt`` layer (a spawned subagent's
role). The global prompt is always prepended.

Capabilities are built from the live tool list rather than written as "if you have X …" prose, so
the agent is never told about a tool it doesn't have (which the model otherwise tries to call) and
never left unaware of one it does. Runtime context (environment, todos, recalled memories) is *not*
in the static prompt — it's injected per turn by the ``EventContext`` plugin / memory manager.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

_GLOBAL_PROMPT_PATH = Path(__file__).resolve().parent / "global_prompt.md"

# One guidance block per optional capability, keyed by a tool name that signals its presence.
# Emitted only when that tool is in the agent's actual tool set.
_CAPABILITY_BLOCKS: list[tuple[str, str]] = [
    (
        "skill",
        "**Skills — activate the relevant one BEFORE you start** (`skill` tool). Skills are "
        "activatable procedures (review, triage, adversarial testing, API bar-raising, issue "
        "first-response, …); each carries the SOP, checklists, background, and resources for that "
        "kind of work, plus the quality bar you're expected to hit. Before any substantial task, "
        "your *first* move is `skill(action=\"list\")` to see what's available, then "
        "`skill(action=\"activate\", name=…)` for the one that matches — this loads its full "
        "instructions into your prompt. Do this **before** you touch the shell, git, or a diff — "
        "not partway through, and not from memory. If a task matches a skill and you didn't "
        "activate it, you're winging it: stop, activate it, and follow the procedure. Pick the "
        "narrowest skill that fits; activate more than one when the work spans them. Only genuinely "
        "trivial asks (a quick question, an admin tweak) need no skill.",
    ),
    (
        "todo",
        "**Todo** (`todo` tool) — plan multi-step work with it and keep it updated; it's "
        "re-surfaced to you each turn so you stay oriented.",
    ),
    (
        "spawn",
        "**Subagents** (`spawn` tool) — delegate a focused, separable subtask to an isolated "
        "subagent. Reach for it to:\n"
        "  - **Break down complex work**: split a big task into parts a subagent can own end to "
        "end, then synthesize their results — instead of trying to hold everything in one context.\n"
        "  - **Manage your own context**: hand a subagent the noisy, token-heavy legwork (reading a "
        "large tree, sifting logs, a wide search) so the bulk stays out of your window; you keep "
        "the distilled answer, not the raw material.\n"
        "  - **Validate your own work**: spawn an independent pass — a review, an adversarial check, "
        "a go/no-go — on something you just produced. A fresh subagent that didn't write the code "
        "catches what you'll rationalize past.\n"
        "Each subagent starts **blind** — it doesn't see this conversation — so its `prompt` must be "
        "self-contained: state the goal, the background, and where to look (paths, ids, commands) so "
        "it can discover the rest with its own tools. Set its role with `system_prompt` (a path to "
        "a role/skill prompt file, or literal text). **Match its model to the task** with `model`: "
        '`"advanced"` (Fable 5) when the quality of the analysis IS the deliverable — adversarial '
        'testing, API bar-raising, deep dives; `"fast"` (Haiku) for simple mechanical legwork; '
        "omit it for the default (Opus). Subagents run in parallel; **you own "
        "synthesizing their results** — don't just relay them. A subagent can fail transiently "
        "(an API error or a corrupted internal state it can't itself recover from); a failed spawn "
        "surfaces to you as an error result. **Retry a failed spawn once or twice** — a retry is a "
        "fresh subagent, so a transient failure usually clears. If it keeps failing the same way, "
        "stop retrying and proceed without that pass: note in your output that it didn't complete "
        "and why, rather than blocking the whole task on it.",
    ),
    (
        "use_github",
        "**GitHub** (`use_github` tool) — read and act on GitHub (issues, PRs, comments) within the "
        "configured guardrails. Confirm before mutating (commenting, pushing, merging) unless told "
        "to proceed.\n"
        "  **Comment format & tone** — a maintainer should get the point in a glance:\n"
        "  - **TL;DR first, ≤~15 visible lines.** Open with a one-line summary; put everything "
        "bulky — full analysis, code, tables, alternatives, reasoning — inside a "
        "`<details><summary>…</summary>` block. Long + collapsed is fine; long + visible is not. "
        "This holds for *every* comment (reviews, answers, proposals), however important the "
        "content feels.\n"
        "  - **One comment, not many.** If you already commented and it needs changes, **edit it** "
        "(`updateIssueComment` with the existing id) — don't stack a second one.\n"
        "  - **Add value or stay silent.** No 'LGTM', no restating the diff, no generic advice, no "
        "status updates. On public `strands-agents/*` repos hold a higher bar: silence beats noise. "
        "Answering a question you were *directly* asked always counts as value.\n"
        "  - **@mention = a notification, not a reference.** Only `@username` when you actually need "
        "that person to act or decide. To merely refer to someone, use backticks (`username`) — "
        "this applies even to the maintainer.\n"
        "  - **Match the room.** Be warm, concise, and specific; respect issue/PR templates when "
        "the repo has them.",
    ),
    (
        "search_memory",
        "**Long-term memory** (`search_memory` / `add_memory`) — durable recall across "
        "conversations (relevant memories are also surfaced automatically). **Search first**: "
        "before non-trivial work in a codebase or domain you've touched before, check what you "
        "already learned — including how your past work there *landed*. **Record what's worth "
        "recalling later**, as you learn it — code facts (how a subsystem works, where a thing "
        "lives, a non-obvious gotcha), procedures (the steps that worked to build/test/deploy "
        "here), preferences (how the user wants things done), and mistakes (what went wrong and "
        "the fix). **Close the feedback loop on your own proposals:** when a human accepts, "
        "dismisses, or pushes back on something you put forward — a review finding, a fix, a "
        "suggestion — record the verdict *and their reasoning*, then **search for it before making "
        "the same kind of call again** and don't re-raise what's already been rejected for this "
        "repo / file / pattern. That's how you stop repeating rejected suggestions and get sharper "
        "over time instead of re-litigating settled calls. Scope each memory so retrieval is "
        "precise — name the repo, file, command, and the pattern it's about — and keep it to one "
        "clear, self-contained fact; never record secrets, transient state, or what's trivially "
        "re-derivable from the code.",
    ),
]


# Guidance for the ephemeral (AgentCore) sandbox: its filesystem does NOT survive across separate
# invocations of the same session, so long/multi-invoke work must persist its state OUT of the
# sandbox. Emitted only when the sandbox is the non-local AgentCore one (the local sandbox is the
# user's real disk, which persists). Applies to ALL work, not any one skill.
_EPHEMERAL_SANDBOX_BLOCK = (
    "## Your sandbox is ephemeral\n"
    "Your shell and files run in a sandbox whose **filesystem does not persist across separate "
    "invocations**. Within a single run it's stable — clone, edit, build, and test freely. But a "
    "*later* invocation on the same task (a follow-up mention, a scheduled re-check, a resumed "
    "job) may land on a **fresh, empty sandbox**: your clones, branches, uncommitted edits, and "
    "installed tools will be gone, even though your conversation memory persists. Plan for that:\n"
    "- **Persist real work in durable stores, not the sandbox.** Push code to a branch on the "
    "remote (you have native git), post findings/decisions to the GitHub thread, and record "
    "durable facts in long-term memory. The sandbox is scratch space, never the system of record.\n"
    "- **Commit and push before you finish, not just at the very end.** For anything spanning more "
    "than a few steps, push a WIP branch as you go so partial progress survives — a run cut short "
    "(timeout, transient error, instance recycle) then resumes from the remote instead of redoing "
    "the work. Use a stable, task-derived branch name so a later invocation finds and continues it.\n"
    "- **On resume, reconcile before redoing.** If memory says you already started but the sandbox "
    "is empty, first check the remote (does your WIP branch exist? what's on it?) and continue from "
    "there — re-clone your own branch — rather than starting the task over from scratch.\n"
    "- **Never assume a file, clone, or install from an earlier turn is still there** — verify, and "
    "re-create it if not."
)


def global_prompt() -> str:
    """The shared global prompt prepended to every agent + subagent."""
    return _GLOBAL_PROMPT_PATH.read_text().strip()


def _capabilities_section(tool_names: Iterable[str]) -> str:
    """Build the '## Capabilities' block for exactly the optional tools present, or '' if none."""
    present = set(tool_names)
    blocks = [text for signal, text in _CAPABILITY_BLOCKS if signal in present]
    if not blocks:
        return ""
    return "## Capabilities\n" + "\n".join(f"- {b}" for b in blocks)


def compose(
    system_prompt: str = "",
    tool_names: Iterable[str] | None = None,
    *,
    ephemeral_sandbox: bool = False,
) -> str:
    """Compose the full system prompt: global prompt, a dynamic capabilities block, then a layer.

    Args:
        system_prompt: Optional role layer placed on top of the global prompt (subagents pass it).
        tool_names: The agent's actual tool names; the capabilities section is built from these so
            the prompt only describes tools the agent really has. ``None`` omits the section.
        ephemeral_sandbox: True when the sandbox is the non-local (AgentCore) one, whose filesystem
            doesn't persist across separate invocations. Adds the ephemeral-sandbox guidance so the
            agent persists work out of the sandbox — applies to all work, not any one skill. Omitted
            for the local sandbox (the user's real disk, which persists).
    """
    parts = [global_prompt()]
    if tool_names is not None:
        caps = _capabilities_section(tool_names)
        if caps:
            parts.append(caps)
    if ephemeral_sandbox:
        parts.append(_EPHEMERAL_SANDBOX_BLOCK)
    if system_prompt and system_prompt.strip():
        parts.append(system_prompt.strip())
    return "\n\n".join(parts)
