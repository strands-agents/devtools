#!/usr/bin/env python3
"""
Strands Beta Agent Runner

A separate agent runner with extended capabilities (skills, sub-agents, etc.).
Reuses shared infrastructure from agent_runner.py — same pipeline, different agent.

Usage: /strands beta <command>
"""

import json
import os
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

DEFAULT_SYSTEM_PROMPT = "You are an autonomous GitHub agent powered by Strands Agents SDK with extended capabilities including agent skills and sub-agent orchestration."


def _get_beta_tools() -> list[Any]:
    """Get tools for the beta agent.

    Starts with all standard tools, then adds beta-only tools.
    This ensures the beta agent is a strict superset of the standard agent.
    """
    tools = _get_all_tools()

    # Add beta-only tools (use_agent is already imported at module level)
    # Check if use_agent is already in the list (it is in the current version)
    tool_names = {getattr(t, '__name__', str(t)) for t in tools}
    if 'use_agent' not in tool_names:
        tools.append(use_agent)

    return tools


def _load_skills_plugin():
    """Load agent skills from the agent-skills directory if available.

    Returns AgentSkills plugin instance or None if skills aren't available.
    Skills are loaded from agent-skills/ which is copied to the working directory
    by the GitHub Action.
    """
    try:
        from strands.vended_plugins.skills import AgentSkills
    except ImportError:
        print("ℹ️ AgentSkills plugin not available (strands.vended_plugins.skills not found)")
        return None

    # Look for skills directory in the working directory
    # The action.yml copies agent-skills/ to the working directory
    possible_paths = [
        Path("agent-skills"),  # Working directory (copied by action.yml)
        Path("devtools/strands-command/agent-skills"),  # Before copy step
    ]

    skills_dir = None
    for path in possible_paths:
        if path.exists() and path.is_dir():
            skills_dir = path
            break

    if skills_dir is None:
        print("ℹ️ No agent-skills directory found (skills not available)")
        return None

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

        system_prompt = os.getenv("INPUT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
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

        # Beta-only: Load agent skills plugin
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
