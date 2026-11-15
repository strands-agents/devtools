#!/usr/bin/env python3
"""
Strands GitHub Agent Runner
A portable agent runner for use in GitHub Actions across different repositories.
"""

import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
from strands import Agent
from strands.models import Model
from strands.telemetry import StrandsTelemetry
from strands.session import SnapshotSessionManager
from strands.storage import S3Storage
from strands.vended_plugins.skills import AgentSkills
from strands.vended_tools import http_request
from strands_harness import create_harness

# Import local GitHub tools we need
from github_tools import (
    add_issue_comment,
    add_issue_labels,
    add_pr_comment,
    create_issue,
    create_pull_request,
    get_issue,
    get_issue_comments,
    get_pull_request,
    get_pr_files,
    get_pr_review_and_comments,
    list_issues,
    list_pull_requests,
    reply_to_review_comment,
    update_issue,
    update_pull_request,
)

# Import local tools we need
from handoff_to_user import handoff_to_user
from notebook import notebook

# Harness built-ins. web_fetch (needs a second summarizer model the consuming repo's role may not
# allow), programmatic_tool_caller and subagent stay off.
STRANDS_BUILTIN_TOOLS = ["shell", "read", "write", "edit"]
# One skill per mode (skills/task-<mode>/SKILL.md), read from this devtools copy, never the checkout.
SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
# The harness contract says to confirm before outward actions, but there is nobody to ask here. The
# harness environment plugin is off (it would inject the untrusted checkout's AGENTS.md as trusted
# context), so name the working directory ourselves.
INSTRUCTIONS = (
    "You are an autonomous GitHub agent powered by Strands Agents SDK.\n\n"
    "Your task is defined by the `{skill}` skill. Activate it with the skills tool before doing "
    "anything else, then follow its instructions.\n\n"
    "This is an unattended CI run: no one is watching it live, so a question only reaches a human "
    "if you post it to GitHub first. handoff_to_user ends the run; a later /strands command resumes "
    "this same session with the answer. The GitHub actions your instructions prescribe are "
    "pre-authorized. Working directory: {cwd} (file tools take absolute paths)."
)


def _send_eval_trigger(session_id: str, eval_type: str) -> None:
    """Send evaluation trigger to SQS queue after agent completion.
    
    Only sends if EVALS_SQS_QUEUE_ARN environment variable is set.
    Derives queue URL from ARN (format: arn:aws:sqs:{region}:{account_id}:{queue_name}).
    
    Args:
        session_id: The unique session ID as stored in Langfuse (may include repo prefix).
        eval_type: The evaluation type (e.g., "reviewer", "implementer").
    """
    queue_arn = os.environ.get("EVALS_SQS_QUEUE_ARN")
    if not queue_arn:
        return
    
    # Parse ARN: arn:aws:sqs:{region}:{account_id}:{queue_name}
    arn_parts = queue_arn.split(":")
    if len(arn_parts) != 6:
        print(f"⚠️ Invalid SQS ARN format: {queue_arn}")
        return
    
    region = arn_parts[3]
    account_id = arn_parts[4]
    queue_name = arn_parts[5]
    queue_url = f"https://sqs.{region}.amazonaws.com/{account_id}/{queue_name}"
    
    try:
        sqs_client = boto3.client("sqs", region_name=region)
        message_body = json.dumps({
            "session_id": session_id,
            "eval_type": eval_type
        })
        sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=message_body
        )
        print(f"✅ Sent eval trigger to SQS: {message_body}")
    except Exception as e:
        print(f"⚠️ Failed to send eval trigger to SQS: {e}")


def _setup_langfuse_telemetry() -> bool:
    """Set up Langfuse telemetry if environment variables are configured.
    
    Returns:
        True if telemetry was successfully configured, False otherwise.
    """
    langfuse_public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    langfuse_host = os.environ.get("LANGFUSE_HOST")
    
    if not all([langfuse_public_key, langfuse_secret_key, langfuse_host]):
        print("ℹ️ Langfuse telemetry not configured (missing environment variables)")
        return False
    
    try:
        langfuse_auth = base64.b64encode(
            f"{langfuse_public_key}:{langfuse_secret_key}".encode()
        ).decode()
        
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{langfuse_host}/api/public/otel"
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {langfuse_auth}"
        
        StrandsTelemetry().setup_otlp_exporter()
        print("✅ Langfuse telemetry configured successfully")
        return True
    except Exception as e:
        print(f"⚠️ Failed to configure Langfuse telemetry: {e}")
        return False


def _get_trace_attributes() -> dict:
    """Build trace attributes from environment context."""
    session_id = os.getenv("SESSION_ID", "")
    github_actor = os.getenv("GITHUB_ACTOR", "")
    github_repository = os.getenv("GITHUB_REPOSITORY", "")
    github_workflow = os.getenv("GITHUB_WORKFLOW", "")
    github_run_id = os.getenv("GITHUB_RUN_ID", "")
    
    # Include repo name in session ID for uniqueness across repos
    # Format: "owner_repo:session-id" (e.g., "strands-agents_sdk-typescript:reviewer-443")
    repo_prefix = github_repository.replace("/", "_") if github_repository else "unknown"
    unique_session_id = f"{repo_prefix}:{session_id}" if session_id else f"{repo_prefix}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    return {
        "session.id": unique_session_id,
        "user.id": github_actor,
        "langfuse.tags": [
            f"repo:{github_repository}",
            f"workflow:{github_workflow}",
            f"run:{github_run_id}",
            "strands-github-agent",
        ],
    }

def _get_all_tools() -> list[Any]:
    return [
        # System tools (shell and file tools come from the harness)
        http_request,

        # GitHub issue tools
        create_issue,
        get_issue,
        update_issue,
        list_issues,
        add_issue_comment,
        add_issue_labels,
        get_issue_comments,

        # GitHub PR tools
        create_pull_request,
        get_pull_request,
        update_pull_request,
        list_pull_requests,
        get_pr_files,
        get_pr_review_and_comments,
        reply_to_review_comment,
        add_pr_comment,

        # Agent tools
        notebook,
        handoff_to_user,
    ]


# Tools that should NOT be available to specific agent modes.
# Reviewer/refiner/release-notes/bug-verifier agents only read and comment —
# they should never create issues or PRs.
_MODE_TOOL_DENYLIST: dict[str, list[Any]] = {
    "reviewer": [create_issue, create_pull_request],
    "refiner": [create_issue, create_pull_request],
    "release-notes": [create_issue, create_pull_request],
    "bug-verifier": [create_issue, create_pull_request],
}


def _resolve_model_config() -> tuple[str | None, str]:
    """Return the Bedrock model id and reasoning effort for this run.

    STRANDS_MODEL_ID and STRANDS_REASONING_EFFORT override the harness defaults (its default
    model and ``"auto"`` effort). An unset or empty value means the default; the harness
    validates the effort against the model.
    """
    model_id = os.environ.get("STRANDS_MODEL_ID", "").strip() or None
    effort = os.environ.get("STRANDS_REASONING_EFFORT", "").strip() or "auto"
    return model_id, effort


def _get_tools_for_mode(mode: str | None) -> list[Any]:
    """Return the tool set filtered by agent mode.

    Modes that should not have access to certain write tools (e.g. reviewer
    should not create issues) have those tools removed.
    """
    all_tools = _get_all_tools()
    if not mode or mode not in _MODE_TOOL_DENYLIST:
        return all_tools
    denied = _MODE_TOOL_DENYLIST[mode]
    return [t for t in all_tools if t not in denied]


def _create_agent(
    model: Model | str | None,
    effort: str,
    skill: str,
    extra_instructions: str,
    tools: list[Any],
    session_id: str,
    s3_bucket: str,
    s3_prefix: str,
    trace_attributes: dict | None = None,
) -> Agent:
    """Build the harness agent for one mode's skill, persisted to S3 under ``{s3_prefix}/``.

    One S3 storage backs both the session snapshots (``session/``) and the context manager's
    stash of offloaded tool results (``context/``), so a resumed run can still resolve the
    references left in its conversation.
    """
    skill_dir = SKILLS_DIR / skill
    if not (skill_dir / "SKILL.md").is_file():
        raise ValueError(f"Unknown skill '{skill}': no {skill_dir}/SKILL.md")
    instructions = INSTRUCTIONS.format(skill=skill, cwd=os.getcwd())
    if extra_instructions:
        instructions += f"\n\n{extra_instructions}"
    storage = S3Storage(bucket=s3_bucket, prefix=s3_prefix)
    return create_harness(
        model=model,
        effort=effort,
        instructions=instructions,
        tools=tools,
        builtin_tools=STRANDS_BUILTIN_TOOLS,
        session=SnapshotSessionManager(session_id, storage=storage, save_latest_on="message"),
        storage=storage,
        skills=AgentSkills(skills=[str(skill_dir)], strict=True),
        # The checkout is untrusted and throwaway: no memory or environment context from it, and
        # no todo_write competing with the notebook the skills mandate.
        builtin_plugins=[],
        memory=False,
        **({"trace_attributes": trace_attributes} if trace_attributes else {}),
    )


def run_agent(query: str, model_id: str | None, effort: str):
    """Run the agent with the provided query, Bedrock model id (None for the harness default), and effort."""
    try:
        # Set up Langfuse telemetry (optional - gracefully degrades if not configured)
        telemetry_enabled = _setup_langfuse_telemetry()
        trace_attributes = _get_trace_attributes() if telemetry_enabled else {}

        # Get tools filtered by agent mode (e.g. reviewer can't create issues)
        agent_mode = os.getenv("AGENT_MODE")
        tools = _get_tools_for_mode(agent_mode)

        skill = os.getenv("INPUT_SKILL") or f"task-{agent_mode}"
        session_id = os.getenv("SESSION_ID")
        s3_bucket = os.getenv("S3_SESSION_BUCKET")
        s3_prefix = os.getenv("GITHUB_REPOSITORY", "")
        if not (s3_bucket and session_id):
            raise ValueError("Both SESSION_ID and S3_SESSION_BUCKET must be set")

        print(f"🤖 Using skill {skill} and session ID: {session_id}")
        agent = _create_agent(
            model_id,
            effort,
            skill,
            os.getenv("INPUT_INSTRUCTIONS", ""),
            tools,
            session_id,
            s3_bucket,
            s3_prefix,
            trace_attributes,
        )

        print("Processing user query...")
        result = agent(query)

        print(f"\n\nAgent Result 🤖\nStop Reason: {result.stop_reason}\nMessage: {json.dumps(result.message, indent=2)}")
        
        # Use the unique session ID from trace attributes (includes repo prefix)
        unique_session_id = trace_attributes.get("session.id", session_id)
        eval_type = session_id.split("-")[0] if "-" in session_id else session_id
        _send_eval_trigger(unique_session_id, eval_type)
    except Exception as e:
        error_msg = f"❌ Agent execution failed: {e}"
        print(error_msg)
        raise e


def main() -> None:
    """Main entry point for the agent runner."""
    try:
        # Read task from command line arguments
        if len(sys.argv) < 2:
            raise ValueError("Task argument is required")

        task = " ".join(sys.argv[1:])
        if not task.strip():
            raise ValueError("Task cannot be empty")

        model_id, effort = _resolve_model_config()
        print(f"🤖 Using model={model_id or 'harness default'} effort={effort}")
        print(f"🤖 Running agent with task: {task}")

        run_agent(task, model_id, effort)

    except Exception as e:
        error_msg = f"Fatal error: {e}"
        print(error_msg)

        sys.exit(1)


if __name__ == "__main__":
    main()
