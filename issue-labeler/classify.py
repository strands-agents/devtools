#!/usr/bin/env python3
"""
Issue classifier for the issue-labeler GitHub Action.

Reads a config file defining valid labels, calls Bedrock for classification,
validates the response against the allowlist, and applies labels via gh CLI.

Security model:
- LLM output is validated against a hardcoded allowlist from the config file.
- Issue content is sanitized and truncated before reaching the LLM.
- The LLM has no tools, no shell access, no GitHub API access.
- Worst case from prompt injection: mislabeling (not arbitrary actions).
"""

import json
import os
import re
import subprocess
import sys

import boto3
import yaml


def load_config(config_path: str) -> dict:
    """Load and validate the labeler config file."""
    with open(config_path) as f:
        config = yaml.safe_load(f)

    if not config or "labels" not in config:
        print("::error::Config must have a 'labels' key with label definitions.")
        sys.exit(1)

    if not isinstance(config["labels"], dict) or len(config["labels"]) == 0:
        print("::error::Config 'labels' must be a non-empty mapping of label_name -> description.")
        sys.exit(1)

    return config


def build_system_prompt(config: dict, max_labels: int) -> str:
    """Build the classification prompt from config."""
    label_lines = []
    for label_name, label_def in config["labels"].items():
        description = label_def if isinstance(label_def, str) else label_def.get("description", "")
        label_lines.append(f"- {label_name}: {description}")

    labels_block = "\n".join(label_lines)

    prompt = f"""You are a GitHub issue classifier. Respond with ONLY a JSON array of label strings. No other text, no explanation, no markdown fences.

Available labels:
{labels_block}

Rules:
1. Assign 1-{max_labels} labels maximum.
2. Only assign labels with clear evidence in the title or body.
3. If unsure between multiple labels, prefer fewer labels over more.
4. Respond with ONLY a JSON array. Example: ["label-one", "label-two"]"""

    custom_instructions = config.get("instructions", "")
    if custom_instructions:
        prompt += f"\n\nAdditional context:\n{custom_instructions}"

    return prompt


def sanitize(text: str, max_len: int) -> str:
    """Remove control characters and truncate."""
    if not text:
        return ""
    cleaned = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return cleaned[:max_len]


def classify_issue(title: str, body: str, system_prompt: str, valid_labels: frozenset) -> list[str]:
    """Call Bedrock to classify the issue, return validated labels."""
    model_id = os.environ.get("MODEL_ID", "anthropic.claude-haiku-4-5-20251001")
    region = os.environ.get("AWS_REGION", "us-west-2")
    max_labels = int(os.environ.get("MAX_LABELS", "3"))

    client = boto3.client("bedrock-runtime", region_name=region)

    user_msg = f"Classify this issue:\n\nTitle: {title}\n\nBody:\n{body}"

    request_body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 150,
        "temperature": 0,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_msg}],
    })

    response = client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=request_body,
    )

    response_body = json.loads(response["body"].read())
    raw_text = response_body["content"][0]["text"].strip()
    print(f"Raw LLM output: {raw_text}")

    try:
        labels = json.loads(raw_text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*?\]", raw_text, re.DOTALL)
        if match:
            try:
                labels = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        else:
            return []

    if not isinstance(labels, list):
        return []

    # SECURITY: Only accept labels from the hardcoded allowlist
    validated = [l for l in labels if isinstance(l, str) and l in valid_labels]
    return validated[:max_labels]


def apply_labels(issue_number: str, labels: list[str]) -> None:
    """Apply labels to the issue using gh CLI."""
    repo = os.environ["GH_REPO"]
    label_csv = ",".join(labels)

    print(f"Applying labels to issue #{issue_number}: {label_csv}")
    result = subprocess.run(
        ["gh", "issue", "edit", issue_number, "--repo", repo, "--add-label", label_csv],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"::error::Failed to apply labels: {result.stderr}")
        sys.exit(1)


def main():
    config_path = os.environ["CONFIG_PATH"]
    max_body_length = int(os.environ.get("MAX_BODY_LENGTH", "1000"))
    max_labels = int(os.environ.get("MAX_LABELS", "3"))

    config = load_config(config_path)

    valid_labels = frozenset(config["labels"].keys())
    print(f"Loaded {len(valid_labels)} valid labels: {sorted(valid_labels)}")

    system_prompt = build_system_prompt(config, max_labels)

    title = sanitize(os.environ.get("ISSUE_TITLE", ""), 200)
    body = sanitize(os.environ.get("ISSUE_BODY", ""), max_body_length)
    issue_number = os.environ["ISSUE_NUMBER"]

    if not title:
        print("No issue title, skipping.")
        sys.exit(0)

    print(f"Classifying issue #{issue_number}: {title[:80]}")

    labels = classify_issue(title, body, system_prompt, valid_labels)

    if not labels:
        print("No valid labels identified, skipping.")
        sys.exit(0)

    apply_labels(issue_number, labels)

    # Write output for downstream steps
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"labels={','.join(labels)}\n")

    print(f"Done: {labels}")


if __name__ == "__main__":
    main()
