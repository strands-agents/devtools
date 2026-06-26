#!/usr/bin/env python3
"""
Issue classifier for the issue-labeler GitHub Action.

Reads a config file defining valid labels, calls Bedrock (via the Strands
SDK) for structured classification, and applies labels via gh CLI.

Security model:
- The LLM returns a structured object whose label field is an enum built
  from the config allowlist, so out-of-allowlist values are rejected by the
  schema rather than by hand.
- Issue content is sanitized and truncated before reaching the LLM.
- The LLM has no tools, no shell access, no GitHub API access.
- Worst case from prompt injection: mislabeling (not arbitrary actions).
"""

import enum
import json
import os
import re
import subprocess
import sys

import yaml
from pydantic import BaseModel, Field
from strands import Agent
from strands.models import BedrockModel


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


def _label_def_dict(label_def) -> dict:
    """Return the dict form of a label definition, or {} for the shorthand string form."""
    return label_def if isinstance(label_def, dict) else {}


def parse_label_type_map(config: dict) -> dict:
    """Map label_name -> native issue type name for labels declaring a `type:`."""
    type_map = {}
    for label_name, label_def in config["labels"].items():
        type_name = _label_def_dict(label_def).get("type")
        if type_name:
            type_map[label_name] = type_name
    return type_map


def parse_field_config(config: dict) -> dict | None:
    """Parse the optional top-level `field:` block into {name, option_map}.

    Returns None when no `field:` block is present. option_map maps
    label_name -> single-select option name for labels declaring an `option:`.
    """
    field_block = config.get("field")
    if field_block is None:
        return None

    if not isinstance(field_block, dict) or not field_block.get("name"):
        print("::error::Config 'field' must be a mapping with a non-empty 'name'.")
        sys.exit(1)

    option_map = {}
    for label_name, label_def in config["labels"].items():
        option_name = _label_def_dict(label_def).get("option")
        if option_name:
            option_map[label_name] = option_name

    return {"name": field_block["name"], "option_map": option_map}


def parse_native_ids(graphql_data: dict) -> dict:
    """Index a resolution-query response into case-insensitive name -> ID maps."""
    repo = graphql_data.get("repository") or {}

    types = {}
    for node in (repo.get("issueTypes") or {}).get("nodes", []):
        types[node["name"].lower()] = node["id"]

    fields = {}
    for node in (repo.get("issueFields") or {}).get("nodes", []):
        if node.get("__typename") != "IssueFieldSingleSelect":
            continue
        options = {opt["name"].lower(): opt["id"] for opt in node.get("options", [])}
        fields[node["name"].lower()] = {"id": node["id"], "options": options}

    return {"types": types, "fields": fields}


def select_type(labels: list, type_map: dict) -> str | None:
    """First classified label that maps to a native type name."""
    for label in labels:
        if label in type_map:
            return type_map[label]
    return None


def select_option(labels: list, option_map: dict) -> str | None:
    """First classified label that maps to a field option name."""
    for label in labels:
        if label in option_map:
            return option_map[label]
    return None


_RESOLVE_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    issueTypes(first: 100) { nodes { id name } }
    issueFields(first: 100) {
      nodes {
        __typename
        ... on IssueFieldSingleSelect { id name options { id name } }
      }
    }
  }
}
"""


def resolve_native_ids(repo: str) -> dict:
    """Query repo-level issue types and single-select fields, return name->ID maps."""
    owner, name = repo.split("/", 1)
    result = subprocess.run(
        ["gh", "api", "graphql",
         "-f", f"query={_RESOLVE_QUERY}",
         "-F", f"owner={owner}", "-F", f"name={name}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"::warning::Failed to resolve native type/field IDs: {result.stderr}")
        return {"types": {}, "fields": {}}
    try:
        data = json.loads(result.stdout)["data"]
    except (ValueError, KeyError) as e:
        print(f"::warning::Could not parse native ID resolution response: {e}")
        return {"types": {}, "fields": {}}
    return parse_native_ids(data)


def get_issue_node_id(issue_number: str, repo: str) -> str | None:
    """Return the GraphQL node ID for an issue (mutations need it, not the number)."""
    result = subprocess.run(
        ["gh", "issue", "view", issue_number, "--repo", repo, "--json", "id", "-q", ".id"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"::warning::Could not fetch issue node ID: {result.stderr}")
        return None
    return result.stdout.strip() or None


def set_issue_type(node_id: str, type_id: str) -> None:
    mutation = (
        "mutation($issueId: ID!, $typeId: ID!) {"
        " updateIssueIssueType(input: {issueId: $issueId, issueTypeId: $typeId})"
        " { issue { id } } }"
    )
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={mutation}",
         "-F", f"issueId={node_id}", "-F", f"typeId={type_id}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"::warning::Failed to set issue type: {result.stderr}")
    else:
        print(f"Set issue type: {type_id}")


def set_issue_field(node_id: str, field_id: str, option_id: str) -> None:
    mutation = (
        "mutation($issueId: ID!, $fieldId: ID!, $optionId: ID!) {"
        " setIssueFieldValue(input: {issueId: $issueId,"
        " issueFields: [{fieldId: $fieldId, singleSelectOptionId: $optionId}]})"
        " { issue { id } } }"
    )
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={mutation}",
         "-F", f"issueId={node_id}", "-F", f"fieldId={field_id}", "-F", f"optionId={option_id}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"::warning::Failed to set issue field: {result.stderr}")
    else:
        print(f"Set issue field {field_id}: {option_id}")


def apply_native_targets(issue_number: str, repo: str, labels: list, type_map: dict, field_config: dict | None, *, native: dict | None = None) -> None:
    """Map classified labels to a native issue type / field option and apply them.

    No-ops (without any network calls) when neither a type nor a field mapping
    could match the classified labels.
    """
    wanted_type = select_type(labels, type_map) if type_map else None
    wanted_option = (
        select_option(labels, field_config["option_map"]) if field_config else None
    )
    if wanted_type is None and wanted_option is None:
        return

    if native is None:
        native = resolve_native_ids(repo)
    node_id = get_issue_node_id(issue_number, repo)
    if not node_id:
        return

    if wanted_type is not None:
        type_id = native["types"].get(wanted_type.lower())
        if type_id:
            set_issue_type(node_id, type_id)
        else:
            print(f"::warning::No native issue type named '{wanted_type}' found on {repo}.")

    if wanted_option is not None:
        field = native["fields"].get(field_config["name"].lower())
        if not field:
            print(f"::warning::No single-select issue field named '{field_config['name']}' found on {repo}.")
        else:
            option_id = field["options"].get(wanted_option.lower())
            if option_id:
                set_issue_field(node_id, field["id"], option_id)
            else:
                print(f"::warning::No option '{wanted_option}' on field '{field_config['name']}'.")


def build_system_prompt(config: dict, max_labels: int) -> str:
    """Build the classification prompt from config."""
    label_lines = []
    for label_name, label_def in config["labels"].items():
        description = label_def if isinstance(label_def, str) else _label_def_dict(label_def).get("description", "")
        label_lines.append(f"- {label_name}: {description}")

    labels_block = "\n".join(label_lines)

    prompt = f"""You are a GitHub issue classifier.

Available labels:
{labels_block}

Rules:
1. Assign at most {max_labels} labels.
2. Only assign labels with clear evidence in the title or body.
3. If unsure between multiple labels, prefer fewer labels over more.
4. If no label clearly applies, return an empty list."""

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


def build_classification_model(valid_labels: frozenset) -> type[BaseModel]:
    """Build a Pydantic model whose label field is an enum of the allowlist.

    SECURITY: the enum *is* the allowlist, so the structured-output schema
    rejects any value the model invents that is not a configured label.
    """
    label_enum = enum.Enum("Label", {name: name for name in sorted(valid_labels)})

    class Classification(BaseModel):
        labels: list[label_enum] = Field(
            default_factory=list,
            description="Labels that apply to the issue, drawn only from the allowed set.",
        )

    return Classification


def classify_issue(title: str, body: str, system_prompt: str, valid_labels: frozenset) -> list[str]:
    """Call Bedrock via Strands to classify the issue, return validated labels."""
    model_id = os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-4-6")
    region = os.environ.get("AWS_REGION", "us-west-2")
    max_labels = int(os.environ.get("MAX_LABELS", "3"))

    model = BedrockModel(
        model_id=model_id,
        region_name=region,
        temperature=0,
        max_tokens=512,
    )
    agent = Agent(model=model, system_prompt=system_prompt)

    classification_model = build_classification_model(valid_labels)
    user_msg = f"Classify this issue:\n\nTitle: {title}\n\nBody:\n{body}"

    try:
        result = agent.structured_output(classification_model, user_msg)
    except Exception as e:  # noqa: BLE001 - classification failure must not fail the workflow
        print(f"::warning::Classification failed: {e}")
        return []

    # Enum members carry the label name as their value; dedupe preserving order.
    seen: set[str] = set()
    labels: list[str] = []
    for member in result.labels:
        name = member.value
        if name not in seen:
            seen.add(name)
            labels.append(name)

    print(f"Classified labels: {labels}")
    return labels[:max_labels]


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
    repo = os.environ["GH_REPO"]

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

    # Additive: for issues only, also set native issue type / language field.
    # Best-effort and strictly non-fatal: labels are already applied above, so
    # any failure here (missing gh, malformed field config, API/permission
    # error) is downgraded to a warning rather than failing the workflow.
    event_name = os.environ.get("EVENT_NAME", "")
    if event_name == "issues":
        try:
            type_map = parse_label_type_map(config)
            field_config = parse_field_config(config)
            if type_map or field_config:
                apply_native_targets(issue_number, repo, labels, type_map, field_config)
        except (Exception, SystemExit) as e:  # noqa: BLE001 - additive step must never fail the workflow
            print(f"::warning::Native type/field update skipped: {e}")

    # Write output for downstream steps
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"labels={','.join(labels)}\n")

    print(f"Done: {labels}")


if __name__ == "__main__":
    main()
