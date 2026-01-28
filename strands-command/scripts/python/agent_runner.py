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
from typing import Any

import boto3
from strands import Agent
from strands.telemetry import StrandsTelemetry
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.session import S3SessionManager
from strands.models.bedrock import BedrockModel
from botocore.config import Config

from strands_tools import http_request, shell

# Import local GitHub tools we need
from github_tools import (
    add_issue_comment,
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
from str_replace_based_edit_tool import str_replace_based_edit_tool

# Strands configuration constants
STRANDS_MODEL_ID = "global.anthropic.claude-opus-4-5-20251101-v1:0"
STRANDS_MAX_TOKENS = 64000
STRANDS_BUDGET_TOKENS = 8000
STRANDS_REGION = "us-west-2"

# Default values for environment variables used only in this file
DEFAULT_SYSTEM_PROMPT = "You are an autonomous GitHub agent powered by Strands Agents SDK."


def _send_eval_trigger(session_id: str) -> None:
    """Send evaluation trigger to SQS queue after agent completion.
    
    Only sends if EVALS_SQS_QUEUE_ARN environment variable is set.
    Derives queue URL from ARN (format: arn:aws:sqs:{region}:{account_id}:{queue_name}).
    Extracts eval_type from the first part of session_id (before the hyphen).
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
    
    eval_type = session_id.split("-")[0] if "-" in session_id else session_id
    
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
        # File editing
        str_replace_based_edit_tool,
        
        # System tools
        shell,
        http_request,
        
        # GitHub issue tools
        create_issue,
        get_issue,
        update_issue,
        list_issues,
        add_issue_comment,
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


def run_agent(query: str):
    """Run the agent with the provided query."""
    try:
        # Set up Langfuse telemetry (optional - gracefully degrades if not configured)
        telemetry_enabled = _setup_langfuse_telemetry()
        trace_attributes = _get_trace_attributes() if telemetry_enabled else {}
        
        # Get tools and create model
        tools = _get_all_tools()
        
        # Create Bedrock model with inlined configuration
        additional_request_fields = {}
        additional_request_fields["anthropic_beta"] = ["interleaved-thinking-2025-05-14"]
        
        additional_request_fields["thinking"] = {
            "type": "enabled",
            "budget_tokens": STRANDS_BUDGET_TOKENS
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
            additional_request_fields=additional_request_fields,
            cache_prompt="default",
            cache_tools="default",
        )
        system_prompt = os.getenv("INPUT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
        session_id = os.getenv("SESSION_ID")
        s3_bucket = os.getenv("S3_SESSION_BUCKET")
        s3_prefix = os.getenv("GITHUB_REPOSITORY", "")

        if s3_bucket and session_id:
            print(f"🤖 Using session manager with session ID: {session_id}")
            session_manager = S3SessionManager(
                session_id=session_id,
                bucket=s3_bucket,
                prefix=s3_prefix,
            )
        else:
            raise ValueError("Both SESSION_ID and S3_SESSION_BUCKET must be set")

        # Create agent with optional trace attributes for Langfuse
        agent_kwargs = {
            "model": model,
            "system_prompt": system_prompt,
            "tools": tools,
            "session_manager": session_manager,
        }
        
        if trace_attributes:
            agent_kwargs["trace_attributes"] = trace_attributes
        
        agent = Agent(**agent_kwargs)

        print("Processing user query...")
        result = agent(query)

        print(f"\n\nAgent Result 🤖\nStop Reason: {result.stop_reason}\nMessage: {json.dumps(result.message, indent=2)}")
        
        _send_eval_trigger(session_id)
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
        print(f"🤖 Running agent with task: {task}")

        run_agent(task)

    except Exception as e:
        error_msg = f"Fatal error: {e}"
        print(error_msg)

        sys.exit(1)


if __name__ == "__main__":
    main()
