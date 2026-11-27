"""CloudFormation custom-resource handler: merge CDK-derived keys into a Secrets Manager secret.

Backs ``BackendStack``'s ``MergeSecretPayload`` custom resource. Never replaces the secret's
current value — reads it, overlays the CDK-derived keys on top, writes it back. Any key a human
added directly (``STRANDLY_GITHUB_TOKEN``, ``STRANDLY_MENTION_ALLOWED_AUTHORS``, …) is therefore
never at risk from a Backend stack deploy, no matter how many times it redeploys.
"""

from __future__ import annotations

import json
from typing import Any

import boto3


def merge_payload(secret_arn: str, new_keys: dict[str, str], client: Any) -> dict[str, str]:
    """Pure-ish core: fetch the secret's current value, overlay ``new_keys``, return the merged dict.

    Split out from :func:`handler` so it's unit-testable with a fake/mocked ``client`` and no
    CloudFormation event shape involved.
    """
    try:
        current_raw = client.get_secret_value(SecretId=secret_arn).get("SecretString")
        current = json.loads(current_raw) if current_raw else {}
    except client.exceptions.ResourceNotFoundException:
        current = {}
    if not isinstance(current, dict):
        current = {}
    current.update(new_keys)
    return current


def handler(event: dict[str, Any], context: Any) -> dict[str, str]:
    """CloudFormation custom-resource entrypoint (CREATE/UPDATE merge; DELETE is a no-op).

    DELETE must never touch the secret — removing this custom resource (e.g. a stack rollback)
    should not affect a live secret's contents.
    """
    physical_id = event.get("PhysicalResourceId") or "merge-secret-payload"
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": physical_id}

    client = boto3.client("secretsmanager")
    props = event["ResourceProperties"]
    secret_arn = props["SecretArn"]
    new_keys = json.loads(props["Payload"])

    merged = merge_payload(secret_arn, new_keys, client)
    client.put_secret_value(SecretId=secret_arn, SecretString=json.dumps(merged))
    return {"PhysicalResourceId": "merge-secret-payload"}
