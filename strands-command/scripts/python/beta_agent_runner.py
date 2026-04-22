#!/usr/bin/env python3
"""
Strands Beta Agent Runner

A separate agent runner with extended capabilities (skills, sub-agents,
programmatic tool calling, etc.). Reuses shared infrastructure from
agent_runner.py — same pipeline, different agent.

Usage: /strands beta <command>
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from strands import Agent
from strands.session import S3SessionManager
from strands.models import BedrockModel, CacheConfig
from botocore.config import Config

from strands_tools import http_request, shell, use_agent

# Reuse shared infrastructure from the standard runner
from agent_runner import (
    _get_all_tools,
    _get_trace_attributes,
    _send_eval_trigger,
    _setup_langfuse_telemetry,
    STRANDS_BUDGET_TOKENS,
    STRANDS_MAX_TOKENS,
    STRANDS_MODEL_ID,
    STRANDS_REGION,
)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    """Load the beta agent system prompt.

    Priority:
    1. INPUT_SYSTEM_PROMPT env var (set by process-input.cjs)
    2. BETA_SYSTEM_PROMPT.md file in agent-skills directory
    3. Minimal fallback
    """
    env_prompt = os.getenv("INPUT_SYSTEM_PROMPT", "").strip()
    if env_prompt:
        return env_prompt

    # Try loading from file
    possible_paths = [
        Path("agent-skills/BETA_SYSTEM_PROMPT.md"),
        Path("devtools/strands-command/agent-skills/BETA_SYSTEM_PROMPT.md"),
    ]

    for path in possible_paths:
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                print(f"✅ System prompt loaded from {path}")
                return content
        except Exception as e:
            print(f"⚠️ Failed to read {path}: {e}")

    return "You are an autonomous GitHub agent powered by Strands Agents SDK with extended capabilities including agent skills, sub-agent orchestration, and programmatic tool calling."


# ---------------------------------------------------------------------------
# Programmatic Tool Caller (local copy from strands-agents/tools#387)
# ---------------------------------------------------------------------------

def _load_programmatic_tool_caller():
    """Try to load programmatic_tool_caller from strands_tools or local copy.

    Priority:
    1. strands_tools.programmatic_tool_caller (when merged into tools package)
    2. Local copy at scripts/python/programmatic_tool_caller.py
    """
    try:
        from strands_tools import programmatic_tool_caller
        print("✅ programmatic_tool_caller loaded from strands_tools")
        return programmatic_tool_caller
    except ImportError:
        pass

    # Try local copy
    try:
        scripts_dir = Path(__file__).parent
        local_ptc = scripts_dir / "programmatic_tool_caller.py"
        if local_ptc.exists():
            import importlib.util
            spec = importlib.util.spec_from_file_location("programmatic_tool_caller", local_ptc)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                print("✅ programmatic_tool_caller loaded from local copy")
                return mod.programmatic_tool_caller
    except Exception as e:
        print(f"⚠️ Failed to load local programmatic_tool_caller: {e}")

    print("ℹ️ programmatic_tool_caller not available")
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _get_beta_tools() -> list[Any]:
    """Get tools for the beta agent.

    Starts with all standard tools, then adds beta-only tools.
    This ensures the beta agent is a strict superset of the standard agent.
    """
    tools = _get_all_tools()

    # Add beta-only tools
    tool_names = {getattr(t, "__name__", str(t)) for t in tools}

    if "use_agent" not in tool_names:
        tools.append(use_agent)

    # Add programmatic tool caller
    ptc = _load_programmatic_tool_caller()
    if ptc is not None:
        tools.append(ptc)

    return tools


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

# Map from command mode → skill name
SKILL_MAP = {
    "adversarial-test": "task-adversarial-tester",
    "release-digest": "task-release-digest",
    "meta-reason": "task-meta-reasoner",
    "reviewer": "task-reviewer",
    "review": "task-reviewer",
    "implementer": "task-implementer",
    "implement": "task-implementer",
    "refiner": "task-refiner",
    "refine": "task-refiner",
    "release-notes": "task-release-notes",
}


def _convert_sops_to_skills(skills_dir: Path, sops_dir: Path) -> int:
    """Convert existing SOP files to SKILL.md format at runtime.

    Reads .sop.md files from the SOPs directory, adds YAML frontmatter,
    and writes them as SKILL.md files in the skills directory.
    No source files are modified — conversion is one-way into the skills dir.

    Returns the number of SOPs converted.
    """
    if not sops_dir.exists():
        return 0

    # SOP name → metadata for frontmatter
    sop_metadata = {
        "task-implementer": {
            "description": "Implement tasks defined in GitHub issues using test-driven development. Write code following existing patterns, create comprehensive tests, generate documentation, and create pull requests for review.",
            "allowed_tools": "shell use_github",
        },
        "task-refiner": {
            "description": "Review and refine feature requests in GitHub issues. Identify ambiguities, post clarifying questions, gather missing information, and prepare issues for implementation.",
            "allowed_tools": "shell use_github",
        },
        "task-release-notes": {
            "description": "Generate high-quality release notes for software releases. Analyze merged PRs between git references, identify major features and bug fixes, extract code examples, and format into well-structured markdown.",
            "allowed_tools": "shell use_github",
        },
        "task-reviewer": {
            "description": "Review code changes in pull requests. Analyze diffs, understand context, and add targeted review comments to improve code quality, maintainability, and adherence to project standards.",
            "allowed_tools": "shell use_github",
        },
    }

    converted = 0
    for sop_file in sops_dir.glob("*.sop.md"):
        # Extract skill name: task-implementer.sop.md → task-implementer
        skill_name = sop_file.stem.replace(".sop", "")
        skill_dir = skills_dir / skill_name

        # Skip if skill already exists (don't overwrite dedicated skills)
        if (skill_dir / "SKILL.md").exists():
            continue

        metadata = sop_metadata.get(skill_name, {})
        description = metadata.get("description", f"Skill converted from {sop_file.name}")
        allowed_tools = metadata.get("allowed_tools", "shell use_github")

        # Read SOP content
        try:
            sop_content = sop_file.read_text(encoding="utf-8")
        except Exception as e:
            print(f"⚠️ Failed to read {sop_file}: {e}")
            continue

        # Build SKILL.md with frontmatter
        skill_content = f"""---
name: {skill_name}
description: {description}
allowed-tools: {allowed_tools}
---
{sop_content}"""

        # Write to skills directory
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")
        converted += 1
        print(f"  ✅ Converted SOP → skill: {skill_name}")

    return converted


def _load_skills_plugin():
    """Load agent skills from the agent-skills directory if available.

    Also converts existing SOPs to skills at runtime (without duplicating source files).
    Returns AgentSkills plugin instance or None if skills aren't available.
    """
    try:
        from strands.vended_plugins.skills import AgentSkills
    except ImportError:
        print("ℹ️ AgentSkills plugin not available (strands.vended_plugins.skills not found)")
        return None

    # Look for skills directory
    possible_paths = [
        Path("agent-skills"),
        Path("devtools/strands-command/agent-skills"),
    ]

    skills_dir = None
    for path in possible_paths:
        if path.exists() and path.is_dir():
            skills_dir = path
            break

    if skills_dir is None:
        print("ℹ️ No agent-skills directory found (skills not available)")
        return None

    # Convert SOPs to skills at runtime
    possible_sop_paths = [
        Path("devtools/strands-command/agent-sops"),
        Path("agent-sops"),
    ]
    for sops_dir in possible_sop_paths:
        if sops_dir.exists():
            converted = _convert_sops_to_skills(skills_dir, sops_dir)
            if converted > 0:
                print(f"✅ Converted {converted} SOPs to skills")
            break

    try:
        plugin = AgentSkills(skills=str(skills_dir))
        skills = plugin.get_available_skills()

        if skills:
            print(f"✅ AgentSkills plugin: {len(skills)} skills loaded")
            for skill in skills:
                print(f"  - {skill.name}: {skill.description[:60]}...")
            return plugin
        else:
            print("⚠️ AgentSkills plugin: no skills found in directory")
            return None
    except Exception as e:
        print(f"⚠️ Failed to load skills: {e}")
        return None


def _activate_skill_for_mode(agent: Agent, mode: str) -> None:
    """Activate the appropriate skill based on the command mode.

    Maps the command mode (e.g., "review", "implement") to a skill name
    and invokes it via agent.tool.skills(). This front-loads the skill
    instructions into the agent's context before it starts working.
    """
    skill_name = SKILL_MAP.get(mode)
    if not skill_name:
        print(f"ℹ️ No skill mapped for mode '{mode}'")
        return

    if "skills" not in agent.tool_names:
        print(f"⚠️ skills tool not available, can't activate '{skill_name}'")
        return

    try:
        agent.tool.skills(skill_name=skill_name, record_direct_tool_call=True)
        print(f"✅ Activated skill: {skill_name}")
    except Exception as e:
        print(f"⚠️ Failed to activate skill '{skill_name}': {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_beta_agent(query: str):
    """Run the beta agent with extended capabilities."""
    try:
        # Shared infrastructure from agent_runner.py
        telemetry_enabled = _setup_langfuse_telemetry()
        trace_attributes = _get_trace_attributes() if telemetry_enabled else {}

        # Beta agent tools (superset of standard)
        tools = _get_beta_tools()

        # Same model configuration as standard agent
        additional_request_fields = {}
        additional_request_fields["anthropic_beta"] = ["interleaved-thinking-2025-05-14"]
        additional_request_fields["thinking"] = {
            "type": "enabled",
            "budget_tokens": STRANDS_BUDGET_TOKENS,
        }

        model = BedrockModel(
            model_id=STRANDS_MODEL_ID,
            max_tokens=STRANDS_MAX_TOKENS,
            region_name=STRANDS_REGION,
            boto_client_config=Config(
                read_timeout=900,
                connect_timeout=900,
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
            cache_config=CacheConfig(strategy="auto"),
            additional_request_fields=additional_request_fields,
            cache_prompt="default",
            cache_tools="default",
        )

        system_prompt = _load_system_prompt()
        session_id = os.getenv("SESSION_ID")
        s3_bucket = os.getenv("S3_SESSION_BUCKET")

        if s3_bucket and session_id:
            print(f"🤖 Using session manager with session ID: {session_id}")
            session_manager = S3SessionManager(
                session_id=session_id,
                bucket=s3_bucket,
                prefix=os.getenv("GITHUB_REPOSITORY", ""),
            )
        else:
            raise ValueError("Both SESSION_ID and S3_SESSION_BUCKET must be set")

        # Beta-only: Load agent skills plugin (includes SOP→skill conversion)
        plugins = []
        skills_plugin = _load_skills_plugin()
        if skills_plugin:
            plugins.append(skills_plugin)

        # Create beta agent
        agent_kwargs = {
            "model": model,
            "system_prompt": system_prompt,
            "tools": tools,
            "session_manager": session_manager,
        }

        if plugins:
            agent_kwargs["plugins"] = plugins

        if trace_attributes:
            agent_kwargs["trace_attributes"] = trace_attributes

        agent = Agent(**agent_kwargs)

        print(f"🧪 Beta agent created with {len(tools)} tools and {len(plugins)} plugins")

        # Auto-activate skill based on command mode
        # The mode is embedded in the session_id by process-input.cjs (e.g., "reviewer-123")
        mode = os.getenv("AGENT_MODE", "")
        if mode:
            _activate_skill_for_mode(agent, mode)

        print("Processing user query...")
        result = agent(query)

        print(f"\n\nAgent Result 🤖\nStop Reason: {result.stop_reason}\nMessage: {json.dumps(result.message, indent=2)}")

        # Eval trigger (shared infrastructure)
        unique_session_id = trace_attributes.get("session.id", session_id)
        eval_type = session_id.split("-")[0] if "-" in session_id else session_id
        _send_eval_trigger(unique_session_id, eval_type)

    except Exception as e:
        error_msg = f"❌ Beta agent execution failed: {e}"
        print(error_msg)
        raise e


def main() -> None:
    """Main entry point for the beta agent runner."""
    try:
        if len(sys.argv) < 2:
            raise ValueError("Task argument is required")

        task = " ".join(sys.argv[1:])
        if not task.strip():
            raise ValueError("Task cannot be empty")
        print(f"🧪 Running beta agent with task: {task}")

        run_beta_agent(task)

    except Exception as e:
        error_msg = f"Fatal error: {e}"
        print(error_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
