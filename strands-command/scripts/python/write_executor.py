#!/usr/bin/env python3
"""Write Executor Script for GitHub Operations.

This script reads JSONL artifact files containing deferred GitHub operations
and executes them using functions from github_tools.py. It's designed to run
after the strands-agent-runner to publish any write commands or commits.
"""

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from github_tools import GitHubOperation

# Import write only github_tools functions for dynamic execution
from github_tools import (
    create_issue,
    update_issue, 
    add_issue_comment,
    create_pull_request,
    update_pull_request,
    reply_to_review_comment,
    add_pr_comment,
)

# Configure structured logging
logging.basicConfig(
    format="%(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()],
    level=logging.INFO
)
logger = logging.getLogger("write_executor")


def read_parsed_input() -> Dict[str, Any] | None:
    """Read parsed input artifact if it exists.
    
    Returns:
        Dictionary with parsed input data or None if not found
    """
    artifact_path = Path("strands-parsed-input.json")
    if not artifact_path.exists():
        logger.debug("Parsed input artifact not found")
        return None
    
    try:
        with open(artifact_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read parsed input: {e}")
        return None


def post_fork_commit_comment(issue_id: int, branch_name: str, head_repo: str, base_repo: str, run_id: str):
    """Post a comment with fork commit instructions.

    Args:
        issue_id: Issue number to comment on
        branch_name: Branch name created by agent
        head_repo: Fork repository name (user/repo)
        base_repo: Base repository name (owner/repo)
        run_id: GitHub Actions workflow run ID
    """
    comment = f"""## 🔀 Fork Changes Ready

The agent has completed its work on your fork. To apply these changes:

```bash
# Create a unique temporary directory and download the artifact
TEMP_DIR=$(mktemp -d)
cd "$TEMP_DIR"

# Download and extract the repository state
gh run download {run_id} -n repository-state -R {base_repo}
tar -xzf repository_state.tar.gz

# Push the changes to your fork
git push origin {branch_name}

# Clean up
cd ~
rm -rf "$TEMP_DIR"
```

**Note:** You'll need the [GitHub CLI (`gh`)](https://cli.github.com/) installed and authenticated to download the artifact.

Alternatively, you can manually download the `repository-state` artifact from the [workflow run](https://github.com/{base_repo}/actions/runs/{run_id}).

This will push the changes to your fork at `{head_repo}`."""

    logger.info(f"Posting fork commit instructions to issue #{issue_id}")
    add_issue_comment(issue_id, comment)


def get_function_mapping() -> Dict[str, Any]:
    """Get mapping of function names to actual functions."""
    return {
        create_issue.tool_name: create_issue,
        update_issue.tool_name: update_issue,
        add_issue_comment.tool_name: add_issue_comment,
        create_pull_request.tool_name: create_pull_request,
        update_pull_request.tool_name: update_pull_request,
        reply_to_review_comment.tool_name: reply_to_review_comment,
        add_pr_comment.tool_name: add_pr_comment,
    }


def process_jsonl_file(file_path: Path, default_issue_id: int | None = None):
    """Process JSONL file and execute operations.
    
    Args:
        file_path: Path to the JSONL artifact file
        default_issue_id: Default issue ID to use for fallback operations
        
    Returns:
        Tuple of (total_operations, successful_operations, failed_operations)
    """
    function_map = get_function_mapping()
    
    logger.info(f"Starting JSONL processing: {file_path}")
    total_ops = 0
    with open(file_path, 'r') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
                
            total_ops += 1
            logger.info(f"Processing operation {total_ops} (line {line_num})")
            
            try:
                # Parse JSONL entry
                operation: GitHubOperation = json.loads(line)
                func_name = operation.get("function")
                args = operation.get('args', [])
                kwargs = operation.get('kwargs', {})
                
                if not func_name:
                    logger.error(f"Line {line_num}: Missing function name")
                    continue
                
                # Get function from mapping
                if func_name not in function_map:
                    logger.error(f"Line {line_num}: Unknown function '{func_name}'")
                    continue
                
                func = function_map[func_name]
                
                # Set default issue ID for create_pull_request if not already set
                if func_name == "create_pull_request" and default_issue_id and not kwargs.get("fallback_issue_id"):
                    kwargs["fallback_issue_id"] = default_issue_id
                
                # Execute function
                logger.info(f"Executing {func_name} with args={args}, kwargs={kwargs}")
                result = func(*args, **kwargs)
                
                logger.info(f"Line {line_num}: Operation {func_name} completed successfully")
                logger.info(f"Function output: {str(result)}")
                    
            except Exception as e:
                logger.error(f"Line {line_num}: Execution error - {e}")
                    
    
    logger.info(f"JSONL processing completed.")


def main():
    """Main entry point for the write executor script."""
    parser = argparse.ArgumentParser(
        description="Execute deferred GitHub operations from JSONL artifact files"
    )
    parser.add_argument(
        "artifact_file",
        help="Path to JSONL artifact file containing deferred operations"
    )
    parser.add_argument(
        "--issue-id",
        type=int,
        help="Default issue ID to use for fallback operations"
    )
    parser.add_argument(
        "--run-id",
        type=str,
        help="GitHub Actions workflow run ID"
    )
    parser.add_argument(
        "--repository",
        type=str,
        help="Repository name in format owner/repo"
    )

    args = parser.parse_args()
    artifact_path = Path(args.artifact_file)
    
    logger.info(f"Write executor started with artifact file: {artifact_path}")
    if args.issue_id:
        logger.info(f"Default issue ID set to: {args.issue_id}")
    
    # Check if file exists
    if not artifact_path.exists():
        logger.warning(f"Artifact file not found: {artifact_path}")
        logger.warning("No deferred operations to execute")
        return
    
    # Check if file is empty
    if artifact_path.stat().st_size == 0:
        logger.info("Artifact file is empty")
        logger.info("No deferred operations to execute")
        return
    
    # Set environment to enable write operations
    os.environ['GITHUB_WRITE'] = 'true'
    logger.info("GitHub write mode enabled")
    
    logger.info(f"Processing deferred operations from: {artifact_path}")
    
    # Process the JSONL file
    process_jsonl_file(artifact_path, args.issue_id)
    
    # Check if this is a fork PR and post commit instructions
    parsed_input = read_parsed_input()
    if parsed_input and args.issue_id and args.run_id and args.repository:
        head_repo = parsed_input.get("head_repo")
        branch_name = parsed_input.get("branch_name")

        if head_repo and branch_name:
            logger.info("Fork PR detected - posting commit instructions")
            post_fork_commit_comment(args.issue_id, branch_name, head_repo, args.repository, args.run_id)
        else:
            logger.debug("Not a fork PR or missing required fields")
    elif parsed_input and args.issue_id and parsed_input.get("head_repo"):
        logger.warning("Fork PR detected but missing run_id or repository - cannot post commit instructions")

if __name__ == "__main__":
    main()
