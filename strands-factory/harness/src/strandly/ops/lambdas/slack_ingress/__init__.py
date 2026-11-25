"""Slack DM ingress — the push-based Slack trigger for the deployed runtime.

API Gateway (HTTP API) → this Lambda: verify the Slack request signature, authorize the DM's
author, dedup the event, and dispatch the deployed AgentCore runtime fire-and-forget. Part of the
``ops`` **strands-free zone** (stdlib + boto3 only — CI-enforced by
``tests/unit/ops/test_import_hygiene.py``).
"""
