#!/usr/bin/env python3
"""One-time backfill of native issue type + language field on existing issues.

Reuses the classification logic from classify.py. For each issue:
- If it already carries a label from the relevant allowlist (e.g. `bug`,
  `python`), map directly from that label -- no LLM call.
- Otherwise classify the issue with the same config the action uses.

Run locally with `gh` authenticated and AWS credentials available for Bedrock.

Examples:
  python3 backfill.py --repo strands-agents/harness-sdk \\
    --type-config /path/to/type.yml --language-config /path/to/language.yml --dry-run
  python3 backfill.py --repo strands-agents/evals --type-config /path/to/type.yml
"""

import argparse
import json
import subprocess
import sys

from classify import (
    apply_native_targets,
    build_system_prompt,
    classify_issue,
    load_config,
    parse_field_config,
    parse_label_type_map,
    resolve_native_ids,
    sanitize,
    select_option,
    select_type,
)


def decide_label(existing_labels, allowlist, classify_fn):
    """Reuse an existing allowlisted label if present, else classify."""
    for label in existing_labels:
        if label in allowlist:
            return label
    classified = classify_fn()
    return classified[0] if classified else None


def list_issues(repo, state):
    """Return all issues (number, title, body, label names) for the repo."""
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", state,
         "--limit", "10000", "--json", "number,title,body,labels"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"::error::Failed to list issues: {result.stderr}")
        sys.exit(1)
    issues = json.loads(result.stdout)
    for issue in issues:
        issue["label_names"] = [lbl["name"] for lbl in issue.get("labels", [])]
    return issues


def main():
    parser = argparse.ArgumentParser(description="Backfill native issue type/field.")
    parser.add_argument("--repo", required=True, help="OWNER/NAME")
    parser.add_argument("--type-config", help="Path to type labeler config (sets native type).")
    parser.add_argument("--language-config", help="Path to language labeler config (sets field).")
    parser.add_argument("--state", choices=["all", "open", "closed"], default="all")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.type_config and not args.language_config:
        parser.error("at least one of --type-config / --language-config is required")

    # Load configs and derive the mappings + classification prompts.
    type_map = {}
    type_allowlist = frozenset()
    type_prompt = None
    type_config = None
    if args.type_config:
        type_config = load_config(args.type_config)
        type_map = parse_label_type_map(type_config)
        type_allowlist = frozenset(type_config["labels"].keys())
        type_prompt = build_system_prompt(type_config, max_labels=1)

    field_config = None
    lang_allowlist = frozenset()
    lang_prompt = None
    lang_config = None
    if args.language_config:
        lang_config = load_config(args.language_config)
        field_config = parse_field_config(lang_config)
        lang_allowlist = frozenset(lang_config["labels"].keys())
        lang_prompt = build_system_prompt(lang_config, max_labels=1)

    issues = list_issues(args.repo, args.state)
    print(f"Backfilling {len(issues)} issue(s) on {args.repo} (dry_run={args.dry_run})")

    # Resolve native type/field IDs once for the whole run (same for every issue).
    native = resolve_native_ids(args.repo)

    for issue in issues:
        number = str(issue["number"])
        title = sanitize(issue.get("title", ""), 200)
        body = sanitize(issue.get("body", ""), 1000)
        existing = issue["label_names"]

        # Determine the effective type/language labels (reuse or classify).
        effective_labels = []
        if args.type_config:
            type_label = decide_label(
                existing, type_allowlist,
                lambda: classify_issue(title, body, type_prompt, type_allowlist),
            )
            if type_label:
                effective_labels.append(type_label)
        if args.language_config:
            lang_label = decide_label(
                existing, lang_allowlist,
                lambda: classify_issue(title, body, lang_prompt, lang_allowlist),
            )
            if lang_label:
                effective_labels.append(lang_label)

        if not effective_labels:
            print(f"#{number}: no type/language signal, skipping")
            continue

        if args.dry_run:
            wanted_type = select_type(effective_labels, type_map) if type_map else None
            wanted_option = (
                select_option(effective_labels, field_config["option_map"]) if field_config else None
            )
            print(f"#{number}: would apply labels={effective_labels} "
                  f"type={wanted_type} field={wanted_option}")
            continue

        apply_native_targets(number, args.repo, effective_labels, type_map, field_config, native=native)
        print(f"#{number}: applied")


if __name__ == "__main__":
    main()
