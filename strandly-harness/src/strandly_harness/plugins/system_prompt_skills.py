"""``SystemPromptSkills`` — keep an active skill's full instructions *resident in the system
prompt* instead of delivering them as a one-off tool result.

Why this instead of the SDK ``AgentSkills`` default
----------------------------------------------------
The SDK ``AgentSkills`` plugin does *progressive disclosure*: skill **metadata** (name +
description) goes in the system prompt and the agent calls a ``skills`` tool to pull a skill's full
instructions into context as a single tool result. That keeps context small. But over a long
multi-turn run those instructions drift out of the model's attention as the conversation grows —
the agent "forgets" a skill it activated several turns ago.

Strandly ships a curated handful of skills it controls, so it keeps an *active* skill's full
instructions in the system prompt where they stay salient every turn, and lets the agent toggle
skills on/off explicitly. Progressive disclosure is preserved: at startup only each skill's
name + description is in the prompt; a skill's full instructions are injected only after the agent
activates it. (Ported from mkmeral/strands-meta-harness#15, mirroring the SDK
``injection_mode="system_prompt"`` design but as a harness-owned plugin against the released SDK.)

The built-in skill *content* and the plugin **builder** (``build_skills_plugin``, which handles
pushing skills into a non-local sandbox) live in :mod:`strandly_harness.skills.loader`; this module is the
plugin itself.

How it works
------------
- Skills are read **through the agent's sandbox** at ``init_agent`` (same contract as the SDK
  plugin: a skill's files must physically exist on the sandbox FS).
- An *active set* of skill names lives in ``agent.state`` (so it persists across runs via the
  session manager and is safe to share one plugin instance across agents). It starts **empty**.
- The active skills' full instructions are rebuilt into a single ``<active_skills>`` block and
  injected before every model call (``BeforeModelCallEvent``), so a toggle takes effect on the
  very next turn. The block is *rebuilt* (not appended), so nothing accumulates and structured
  system-prompt blocks / cache points are preserved.
- One tool is exposed: ``skill(action, name, path)`` with ``action`` in ``activate`` /
  ``deactivate`` / ``list`` / ``load`` / ``unload``.
- **Dynamic skills** (``load`` / ``unload``): beyond the built-ins, the agent can register skills
  from any folder it can reach *through its sandbox* — e.g. a ``skills/`` or ``.skills/``
  directory inside a repo it just cloned (the harness-sdk monorepo case: work on a repo *with*
  that repo's own skills). ``skill(action="load", path=...)`` loads every skill under that folder
  and merges it into this same plugin: dynamic skills appear in ``<active_skills>``, are
  activated/deactivated with the same tool, and their optional ``GOALS.md`` feeds the goal-loop
  critic exactly like a built-in's. Loading the same path again *refreshes* it (removed skills
  disappear, edits are picked up); ``unload`` drops a path's skills and deactivates them. Loaded
  paths persist in ``agent.state`` (``dynamic_paths``), so a fresh agent over the same session
  re-loads them at ``init_agent`` — fail-soft: a path that no longer exists (e.g. a brand-new
  sandbox without the clone) just logs and loads nothing until the agent re-clones + re-loads.
  Guardrail: a dynamic skill may **not** shadow a built-in — name collisions with the curated
  built-in set are skipped with a warning (repo content must never silently replace the harness's
  own procedures).
- A skill directory MAY also contain an optional ``GOALS.md`` (sibling of ``SKILL.md``). Unlike
  ``SKILL.md`` — the *actor-facing* procedure injected into the system prompt — ``GOALS.md``
  holds **critic-facing acceptance criteria**: the things we explicitly want the goal-loop
  critic to verify when that skill is active. It is NOT injected into the actor's prompt; the
  active skills' goals are stashed in ``agent.state`` and read by the goal-loop critic
  (``plugins/goal.py`` via :func:`active_skill_goals`) so we can "tell the critic exactly what
  to check" per activated skill. Crucially, the critic runs once at the *end* of the turn, but a
  long-horizon turn may activate a skill, finish its work, then deactivate it and activate another
  (e.g. implement -> review). So we also track every skill *engaged at any point during the turn*
  (``engaged_this_turn``, reset each turn) and the critic grades against the union (skills active
  now + skills engaged this turn) -- a skill whose work happened this turn is verified even if it
  was toggled off before the turn ended.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

from strands import tool
from strands.hooks import BeforeInvocationEvent, BeforeModelCallEvent
from strands.plugins import Plugin, hook
from strands.types.content import SystemContentBlock
from strands.vended_plugins.skills import Skill

if TYPE_CHECKING:
    from strands.agent.agent import Agent
    from strands.sandbox.base import Sandbox

logger = logging.getLogger(__name__)

_STATE_KEY = "system_prompt_skills"


def _find_skill_md_name(entries: list[Any]) -> str | None:
    """Return the SKILL.md filename among sandbox dir entries (prefers ``SKILL.md``)."""
    for name in ("SKILL.md", "skill.md"):
        if any(not e.is_dir and e.name == name for e in entries):
            return name
    return None


async def load_skills_via_sandbox(
    sandbox: Sandbox, paths: list[str], *, strict: bool = False
) -> dict[str, Skill]:
    """Load skills from sandbox ``paths`` into a ``{name: Skill}`` map (most recent wins).

    Mirrors the SDK ``AgentSkills`` loading contract: a path may be a SKILL.md file, a skill
    directory, or a parent directory of skill subdirectories. Files are read through the sandbox
    (so they must exist on the sandbox FS). Per-path failures are logged and skipped so one bad
    skill never aborts its siblings.
    """
    skills: dict[str, Skill] = {}

    async def load_one(skill_dir: str, md_path: str) -> None:
        try:
            skill = Skill.from_content(await sandbox.read_text(md_path), strict=strict)
            skill.path = Path(skill_dir)
            if skill.path.name != skill.name:
                msg = "name=<%s>, directory=<%s> | skill name does not match parent directory name"
                if strict:
                    raise ValueError(msg % (skill.name, skill.path.name))
                logger.warning(msg, skill.name, skill.path.name)
            if skill.name in skills:
                logger.warning(
                    "name=<%s> | duplicate skill name, overwriting previous skill", skill.name
                )
            skills[skill.name] = skill
        except Exception as e:  # noqa: BLE001 — resilience: skip a bad skill, keep the rest
            logger.warning("path=<%s> | failed to load skill: %s", skill_dir, e)

    for raw in paths:
        path = str(raw)
        try:
            entries = await sandbox.list_files(path)
        except Exception:  # noqa: BLE001 — not a dir: maybe a direct SKILL.md path
            if path.lower().endswith("skill.md"):
                slash = path.rfind("/")
                await load_one("." if slash == -1 else path[:slash], path)
            else:
                logger.warning(
                    "path=<%s> | skill source does not exist or is not a valid path", path
                )
            continue

        md_name = _find_skill_md_name(entries)
        if md_name:
            await load_one(path, f"{path}/{md_name}")
            continue

        # Parent directory: load each subdirectory that itself contains a skill.
        subdirs = [e for e in entries if e.is_dir]
        if not subdirs:
            # No SKILL.md here and no subdirectories: either an empty/wrong path, or the backend
            # mis-reported directory entries as files (see agentcore_sandbox._block_is_dir — a bug
            # there silently empties the skill set). Surface it rather than returning {} quietly.
            logger.warning(
                "path=<%s>, entries=<%d> | no SKILL.md and no subdirectories detected; "
                "no skills will load from this path",
                path,
                len(entries),
            )
        for entry in sorted(subdirs, key=lambda e: e.name):
            child = f"{path}/{entry.name}"
            try:
                child_entries = await sandbox.list_files(child)
            except Exception as e:  # noqa: BLE001
                logger.warning("path=<%s> | failed to list skill dir: %s", child, e)
                continue
            child_md = _find_skill_md_name(child_entries)
            if child_md:
                await load_one(child, f"{child}/{child_md}")
            else:
                logger.debug("path=<%s> | subdirectory has no SKILL.md; skipping", child)

    return skills


async def load_goals_via_sandbox(
    sandbox: Sandbox, skills: dict[str, Skill]
) -> dict[str, str]:
    """Load the optional ``GOALS.md`` sitting next to each skill's ``SKILL.md``.

    ``GOALS.md`` is **critic-facing** (acceptance criteria), not actor-facing — so it is read here
    but never injected into the actor's system prompt. Returns ``{skill_name: goals_text}`` for
    every skill that has a non-empty ``GOALS.md`` in its directory; skills without one are simply
    absent from the map. A per-skill read failure is logged and skipped (a missing/garbled
    ``GOALS.md`` must never abort skill loading).

    ``skills`` is the map returned by :func:`load_skills_via_sandbox`; each :class:`Skill` carries
    the directory it was loaded from in ``skill.path``, which is where we look for the sibling
    ``GOALS.md`` (read through the same sandbox, so it works identically locally and on AgentCore).
    """
    goals: dict[str, str] = {}
    for name, skill in skills.items():
        if skill.path is None:
            continue
        goals_path = f"{str(skill.path).rstrip('/')}/GOALS.md"
        try:
            text = (await sandbox.read_text(goals_path)).strip()
        except Exception:  # noqa: BLE001 — GOALS.md is optional; absence/failure is non-fatal
            continue
        if text:
            goals[name] = text
    return goals


def active_skill_goals(agent: Agent) -> dict[str, str]:
    """Return ``{skill_name: goals_text}`` for skills relevant to the critic this turn.

    "Relevant" is the **union** of the skills active *right now* and every skill *engaged at any
    point during the current turn* (``engaged_this_turn``). The goal-loop critic runs once at the
    end of the turn; on a long-horizon turn the agent may activate a skill, do its work, then
    deactivate it before the turn ends (e.g. implement-then-review). Reading only the
    currently-active set would silently skip that skill's acceptance criteria, so we include the
    per-turn engaged set too.

    Reads those sets plus the loaded ``GOALS.md`` map that :class:`SystemPromptSkills` stashed in
    ``agent.state``. This is the seam the goal-loop critic uses to discover "what we told it to
    check" **without** a reference to the plugin instance (it only has the agent). Returns an empty
    dict when no relevant skill shipped a ``GOALS.md`` or the plugin never initialized.
    """
    data = agent.state.get(_STATE_KEY)
    if not isinstance(data, dict):
        return {}
    goals = data.get("goals") or {}
    if not isinstance(goals, dict):
        return {}
    # The critic runs once at end-of-turn, but a long-horizon turn may activate a skill, do its
    # work, then deactivate it before the turn ends (e.g. implement-then-review). Grading only the
    # currently-active set would silently skip that skill's GOALS.md. So we union the set active
    # *now* with every skill *engaged at any point this turn*; both contribute their criteria.
    active = data.get("active") or []
    engaged = data.get("engaged_this_turn") or []
    names: list[str] = []
    for name in [*active, *engaged]:
        if name not in names:
            names.append(name)
    return {name: goals[name] for name in names if name in goals}


class SystemPromptSkills(Plugin):
    """Keep active skills' full instructions resident in the system prompt (toggle-able)."""

    name = "strandly-system-prompt-skills"

    def __init__(
        self,
        paths: list[str],
        *,
        activate_by_default: list[str] | None = None,
        strict: bool = False,
    ) -> None:
        """Initialize the plugin.

        Args:
            paths: Skill source paths, read through the agent's sandbox.
            activate_by_default: Skill names active at startup. Defaults to ``None`` → **none**
                active (progressive disclosure: only metadata is in the prompt until the agent
                activates a skill).
            strict: Raise (vs. warn) on skill validation issues.
        """
        self._paths = paths
        self._activate_by_default = activate_by_default
        self._strict = strict
        self._skills: dict[str, Skill] = {}
        self._goals: dict[str, str] = {}
        # Names of the packaged built-in skills (loaded from ``paths``). Dynamic loads may never
        # shadow these — see ``_merge_dynamic``.
        self._builtin_names: set[str] = set()
        # skill name -> the dynamic path it was loaded from (built-ins are absent from this map).
        self._dynamic_origin: dict[str, str] = {}
        self._loaded = False
        super().__init__()

    # ---- lifecycle ---------------------------------------------------------------------------

    async def init_agent(self, agent: Agent) -> None:
        """Load skills through the agent's sandbox and seed the active set in agent state.

        Built-ins load first (from the constructor ``paths``), then every *dynamic* path the agent
        registered via ``skill(action="load", ...)`` in a previous run/turn is re-loaded from
        ``agent.state`` (fail-soft: a stale path — e.g. a fresh sandbox without the clone — just
        logs and contributes nothing until the agent re-clones and re-loads it).
        """
        self._skills = await load_skills_via_sandbox(
            agent.sandbox, self._paths, strict=self._strict
        )
        self._builtin_names = set(self._skills)
        self._dynamic_origin = {}
        for dyn_path in self._dynamic_paths(agent):
            loaded = await load_skills_via_sandbox(agent.sandbox, [dyn_path], strict=False)
            added, skipped = self._merge_dynamic(loaded, dyn_path)
            if added or skipped:
                logger.info(
                    "path=<%s>, added=<%d>, skipped=<%d> | re-loaded dynamic skills from state",
                    dyn_path, len(added), len(skipped),
                )
        # Optional, critic-facing acceptance criteria sitting next to each SKILL.md. Loaded here
        # but never injected into the actor's prompt; stashed in state for the goal-loop critic.
        self._goals = await load_goals_via_sandbox(agent.sandbox, self._skills)
        self._loaded = True
        if not self._skills:
            logger.warning(
                "paths=<%s> | no skills were loaded; system_prompt skills plugin has nothing to "
                "inject (check the skills push to the sandbox and that listed entries report "
                "is_dir correctly)",
                self._paths,
            )
        else:
            logger.info(
                "paths=<%s>, count=<%d> | loaded skills: %s",
                self._paths,
                len(self._skills),
                ", ".join(sorted(self._skills)),
            )
        # Seed the active set only once (don't clobber a set restored from a session).
        if self._get_state(agent, "active") is None:
            requested = self._activate_by_default or []
            active = [n for n in requested if n in self._skills]
            missing = [n for n in requested if n not in self._skills]
            if missing:
                logger.warning(
                    "activate_by_default names not found and skipped: %s", ", ".join(missing)
                )
            self._set_state(agent, "active", active)
        # Stash the loaded goals in state every init (they are derived from packaged content,
        # not user toggles, so always refresh — unlike the active set which we must not clobber).
        self._set_state(agent, "goals", dict(self._goals))
        logger.debug("skill_count=<%d> | system_prompt skills initialized", len(self._skills))

    # ---- tools -------------------------------------------------------------------------------

    @tool(context=True, name="skill")
    async def skill(
        self, action: str, name: str = "", path: str = "", *, tool_context: Any = None
    ) -> str:
        """Manage which skills' full instructions are resident in your system prompt.

        Skills follow progressive disclosure: at startup you see only each skill's *name +
        description*. Activate one to load its full instructions into your system prompt (they
        stay resident every turn until you deactivate it); deactivate to reclaim context.

        Beyond the built-in skills, you can register skills from any folder in your workspace
        with ``action="load"`` — e.g. after cloning a repo that ships its own skills (a ``skills/``
        or ``.skills/`` directory with ``<skill-name>/SKILL.md`` inside). Loaded skills behave
        exactly like built-ins (activate/deactivate/list) and are remembered across turns; loading
        the same path again refreshes it. Built-in skills cannot be overridden.

        Args:
            action: One of ``"activate"``, ``"deactivate"``, ``"list"``, ``"load"``, ``"unload"``.
                - ``activate``   — load ``name``'s full instructions into your system prompt.
                - ``deactivate`` — remove ``name``'s instructions to reclaim context.
                - ``list``       — show all available skills, their origin, and which are active.
                - ``load``       — register every skill under ``path`` (a skill dir, a parent dir
                  of skill dirs, or a direct SKILL.md path). Re-loading a path refreshes it.
                - ``unload``     — remove the skills previously loaded from ``path`` (they are
                  deactivated too).
            name: Skill name to act on (required for ``activate``/``deactivate``; ignored
                otherwise). See the available-skills listing in your system prompt.
            path: Folder (or SKILL.md) to load skills from / unload skills of (required for
                ``load``/``unload``; ignored otherwise). Resolved inside your sandbox, so use the
                same paths as with bash/file_editor.
        """
        agent = getattr(tool_context, "agent", None)
        if agent is None:  # pragma: no cover - context always injected at runtime
            return "Error: the skill tool requires agent context."

        action = (action or "").strip().lower()
        if action == "list":
            return self._list_skills(agent)

        if action in ("load", "unload"):
            norm = (path or "").strip().rstrip("/")
            if not norm:
                return f"Error: action '{action}' requires a 'path' (a folder inside your sandbox)."
            if action == "load":
                return await self._load_path(agent, norm)
            return self._unload_path(agent, norm)

        if action in ("activate", "deactivate"):
            if not name:
                return f"Error: action '{action}' requires a 'name'. Available skills: {', '.join(self._skills) or '(none)'}"
            if name not in self._skills:
                return f"Skill '{name}' not found. Available skills: {', '.join(self._skills) or '(none)'}"
            active = self._active(agent)
            if action == "activate":
                if name in active:
                    return f"Skill '{name}' is already active."
                active.append(name)
                self._set_state(agent, "active", active)
                self._mark_engaged(agent, name)
                return f"Activated skill '{name}'. Its instructions are now in your system prompt."
            # deactivate
            if name not in active:
                return f"Skill '{name}' is not active."
            active.remove(name)
            self._set_state(agent, "active", active)
            return f"Deactivated skill '{name}'."

        return (
            f"Error: unknown action '{action}'. Use action='activate', 'deactivate', 'list', "
            "'load', or 'unload'."
        )

    def _list_skills(self, agent: Agent) -> str:
        """Human-readable listing of all skills with their active state and origin."""
        if not self._skills:
            return "No skills are available."
        active = set(self._active(agent))
        lines = []
        for n, sk in self._skills.items():
            mark = "active" if n in active else "inactive"
            origin = self._dynamic_origin.get(n)
            suffix = f" (loaded from {origin})" if origin else ""
            lines.append(f"- {n} [{mark}]{suffix}: {sk.description}")
        return "Available skills:\n" + "\n".join(lines)

    # ---- dynamic loading (skills from local folders, e.g. a cloned repo) ---------------------

    def _dynamic_paths(self, agent: Agent) -> list[str]:
        """The dynamic skill paths registered via ``load``, persisted in agent state."""
        return list(self._get_state(agent, "dynamic_paths") or [])

    def _merge_dynamic(
        self, loaded: dict[str, Skill], path: str
    ) -> tuple[list[str], list[str]]:
        """Merge freshly loaded skills from ``path`` into the plugin's skill map.

        Built-in names are never overridden (skipped with a warning — external repo content must
        not silently replace the harness's curated procedures). A collision *between* dynamic
        paths keeps the most recent load, matching the loader's own most-recent-wins semantics.
        Returns ``(added_names, skipped_builtin_names)``.
        """
        added: list[str] = []
        skipped: list[str] = []
        for skill_name, skill in loaded.items():
            if skill_name in self._builtin_names:
                skipped.append(skill_name)
                logger.warning(
                    "name=<%s>, path=<%s> | dynamic skill shadows a built-in; skipped",
                    skill_name, path,
                )
                continue
            prev = self._dynamic_origin.get(skill_name)
            if prev is not None and prev != path:
                logger.warning(
                    "name=<%s>, previous=<%s>, path=<%s> | dynamic skill overrides one from "
                    "another path (most recent wins)",
                    skill_name, prev, path,
                )
            self._skills[skill_name] = skill
            self._dynamic_origin[skill_name] = path
            added.append(skill_name)
        return added, skipped

    def _drop_path_skills(self, path: str) -> list[str]:
        """Remove every skill whose origin is ``path`` from the maps; return their names."""
        names = [n for n, p in self._dynamic_origin.items() if p == path]
        for n in names:
            self._skills.pop(n, None)
            self._dynamic_origin.pop(n, None)
            self._goals.pop(n, None)
        return names

    def _prune_active(self, agent: Agent, removed: list[str]) -> list[str]:
        """Drop ``removed`` names from the active set; return the ones that were active."""
        if not removed:
            return []
        active = self._active(agent)
        deactivated = [n for n in active if n in removed]
        if deactivated:
            self._set_state(agent, "active", [n for n in active if n not in removed])
        return deactivated

    async def _load_path(self, agent: Agent, path: str) -> str:
        """Handle ``skill(action="load", path=...)``: (re-)load skills from a sandbox folder."""
        # Refresh semantics: drop what this path contributed before, then load current disk state
        # (deleted skills disappear, edited ones are picked up).
        removed = self._drop_path_skills(path)
        loaded = await load_skills_via_sandbox(agent.sandbox, [path], strict=False)
        added, skipped = self._merge_dynamic(loaded, path)

        # Goals for the newly loaded skills (critic-facing; never in the actor prompt).
        if added:
            new_goals = await load_goals_via_sandbox(
                agent.sandbox, {n: self._skills[n] for n in added}
            )
            self._goals.update(new_goals)
        self._set_state(agent, "goals", dict(self._goals))

        # Anything that vanished on refresh must also leave the active set.
        gone = [n for n in removed if n not in added]
        deactivated = self._prune_active(agent, gone)

        # Persist the path only while it actually contributes skills.
        paths = self._dynamic_paths(agent)
        if added and path not in paths:
            self._set_state(agent, "dynamic_paths", [*paths, path])
        elif not added and path in paths:
            self._set_state(agent, "dynamic_paths", [p for p in paths if p != path])

        if not added:
            parts = [
                f"No skills found at '{path}'. Expected a skill directory (containing SKILL.md), "
                "a parent directory of skill directories, or a SKILL.md path."
            ]
            if skipped:
                parts.append(
                    f"Skipped (name collides with a built-in skill): {', '.join(sorted(skipped))}."
                )
            if gone:
                parts.append(
                    f"Previously loaded from this path and now removed: {', '.join(sorted(gone))}."
                )
            return " ".join(parts)

        parts = [f"Loaded {len(added)} skill(s) from '{path}': {', '.join(sorted(added))}."]
        if skipped:
            parts.append(
                f"Skipped (name collides with a built-in skill): {', '.join(sorted(skipped))}."
            )
        if gone:
            parts.append(f"Removed on refresh: {', '.join(sorted(gone))}.")
        if deactivated:
            parts.append(f"Deactivated: {', '.join(sorted(deactivated))}.")
        parts.append('Activate one with skill(action="activate", name=...).')
        return " ".join(parts)

    def _unload_path(self, agent: Agent, path: str) -> str:
        """Handle ``skill(action="unload", path=...)``: drop a path's skills + deactivate them."""
        removed = self._drop_path_skills(path)
        paths = self._dynamic_paths(agent)
        if path in paths:
            self._set_state(agent, "dynamic_paths", [p for p in paths if p != path])
        if not removed:
            known = sorted(set(self._dynamic_origin.values()))
            hint = f" Currently loaded paths: {', '.join(known)}." if known else ""
            return f"No skills are loaded from '{path}'.{hint}"
        deactivated = self._prune_active(agent, removed)
        self._set_state(agent, "goals", dict(self._goals))
        msg = f"Unloaded {len(removed)} skill(s) from '{path}': {', '.join(sorted(removed))}."
        if deactivated:
            msg += f" Deactivated: {', '.join(sorted(deactivated))}."
        return msg

    # ---- injection ---------------------------------------------------------------------------

    @hook  # type: ignore[call-overload]  # SDK hook() overloads don't model bound (self, event) methods
    def reset_engaged_this_turn(self, event: BeforeInvocationEvent) -> None:
        """Reset the per-turn "engaged" set at the start of each agent invocation.

        ``engaged_this_turn`` accumulates every skill activated during a single turn so the
        end-of-turn critic can grade skills that were toggled on then off mid-turn (see
        :func:`active_skill_goals`). It is cleared here so it tracks exactly the *current* turn and
        never bleeds stale entries across turns. Seed it with whatever is active at turn start so a
        skill carried over from a prior turn (and used but not re-activated) is still graded.
        """
        active = self._active(event.agent)
        self._set_state(event.agent, "engaged_this_turn", list(active))

    @hook  # type: ignore[call-overload]  # SDK hook() overloads don't model bound (self, event) methods
    def reinject(self, event: BeforeModelCallEvent) -> None:
        """Rebuild the active-skills block and inject it into the system prompt before each call.

        Rebuilt (not appended) every call: the previously injected block is removed by exact match
        and a fresh one appended, so toggling takes effect next turn and nothing accumulates.
        """
        agent = event.agent
        block_text = self._render_block(agent)
        last = self._get_state(agent, "last_injected")

        content = agent.system_prompt_content
        if content is not None:
            blocks: list[SystemContentBlock] = list(content)
            if last is not None:
                injected: SystemContentBlock = {"text": last}
                if injected in blocks:
                    blocks.remove(injected)
                else:
                    logger.warning(
                        "previously injected skills block not found in system prompt, re-appending"
                    )
            blocks.append({"text": block_text})
            agent.system_prompt = blocks
            self._set_state(agent, "last_injected", block_text)
        else:
            current = agent.system_prompt or ""
            if last is not None and last in current:
                current = current.replace(last, "")
            injection = f"\n\n{block_text}" if current else block_text
            agent.system_prompt = f"{current}{injection}" if current else block_text
            self._set_state(agent, "last_injected", injection if current else block_text)

    def _render_block(self, agent: Agent) -> str:
        """Render the ``<active_skills>`` system-prompt block from the active set.

        Lists every loaded skill's name+description (so the model knows what it can toggle) and
        embeds the *full instructions* of the active ones.
        """
        if not self._skills:
            return "<active_skills>\nNo skills are currently available.\n</active_skills>"
        active = set(self._active(agent))
        lines: list[str] = ["<active_skills>"]
        for name, skill in self._skills.items():
            is_active = name in active
            lines.append(f'<skill name="{escape(name)}" active="{str(is_active).lower()}">')
            lines.append(f"<description>{escape(skill.description)}</description>")
            if is_active and skill.instructions:
                lines.append(f"<instructions>\n{escape(skill.instructions)}\n</instructions>")
            if skill.path is not None:
                lines.append(f"<location>{escape(str(skill.path) + '/SKILL.md')}</location>")
            lines.append("</skill>")
        lines.append(
            "Activate a skill with skill(action=\"activate\", name=...) to load its full "
            "instructions here; skill(action=\"deactivate\", name=...) removes them. If a repo in "
            "your workspace ships its own skills (e.g. a skills/ or .skills/ folder), register "
            "them with skill(action=\"load\", path=...); skill(action=\"unload\", path=...) removes "
            "them again."
        )
        lines.append("</active_skills>")
        return "\n".join(lines)

    # ---- state helpers -----------------------------------------------------------------------

    def _active(self, agent: Agent) -> list[str]:
        return list(self._get_state(agent, "active") or [])

    def _mark_engaged(self, agent: Agent, name: str) -> None:
        """Record that ``name`` was activated during the current turn (idempotent).

        Feeds :func:`active_skill_goals` so the end-of-turn critic verifies a skill's GOALS.md even
        if the agent activated it, used it, then deactivated it before the turn finished.
        """
        engaged = list(self._get_state(agent, "engaged_this_turn") or [])
        if name not in engaged:
            engaged.append(name)
            self._set_state(agent, "engaged_this_turn", engaged)

    def _get_state(self, agent: Agent, key: str) -> Any:
        data = agent.state.get(_STATE_KEY)
        return data.get(key) if isinstance(data, dict) else None

    def _set_state(self, agent: Agent, key: str, value: Any) -> None:
        data = agent.state.get(_STATE_KEY)
        if data is not None and not isinstance(data, dict):
            raise TypeError(
                f"expected dict for state key '{_STATE_KEY}', got {type(data).__name__}"
            )
        data = dict(data) if isinstance(data, dict) else {}
        data[key] = value
        agent.state.set(_STATE_KEY, data)

    # ---- introspection (for tests / programmatic control) -----------------------------------

    def get_loaded_skills(self) -> list[Skill]:
        """Return the skills loaded for the agent (after ``init_agent``)."""
        return list(self._skills.values())

    def get_active_skills(self, agent: Agent) -> list[str]:
        """Return the names of currently-active skills for ``agent``."""
        return self._active(agent)

    def get_dynamic_skills(self) -> dict[str, str]:
        """Return ``{skill_name: source_path}`` for dynamically loaded (non-built-in) skills."""
        return dict(self._dynamic_origin)

    def get_loaded_goals(self) -> dict[str, str]:
        """Return the ``{skill_name: goals_text}`` map loaded from ``GOALS.md`` files."""
        return dict(self._goals)

    def get_active_goals(self, agent: Agent) -> dict[str, str]:
        """Return the ``GOALS.md`` text for the skills currently active on ``agent``."""
        return active_skill_goals(agent)
