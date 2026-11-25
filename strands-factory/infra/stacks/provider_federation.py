"""Non-secret, opt-in direct provider federation settings (no AWS or provider API calls).

Provider registration and AWS outbound federation enablement are one-time, manual prerequisites.
This module only validates their public identifiers; it never accepts or resolves provider keys.
"""

from __future__ import annotations

import json
import re

# Google exchanges a signed sts:GetCallerIdentity request, so it needs no web identity audience.
PROVIDER_AUDIENCES = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
}
PROVIDER_ENVIRONMENT = {
    "anthropic": {
        "federation_rule_id": "ANTHROPIC_FEDERATION_RULE_ID",
        "organization_id": "ANTHROPIC_ORGANIZATION_ID",
        "service_account_id": "ANTHROPIC_SERVICE_ACCOUNT_ID",
        "workspace_id": "ANTHROPIC_WORKSPACE_ID",
    },
    "openai": {
        "identity_provider_id": "OPENAI_IDENTITY_PROVIDER_ID",
        "service_account_id": "OPENAI_SERVICE_ACCOUNT_ID",
    },
    "google": {
        "project_id": "GOOGLE_CLOUD_PROJECT",
        "project_number": "GOOGLE_WIF_PROJECT_NUMBER",
        "pool_id": "GOOGLE_WIF_POOL_ID",
        "provider_id": "GOOGLE_WIF_PROVIDER_ID",
        "location": "GOOGLE_CLOUD_LOCATION",
    },
}

# Anthropic documents these prefixes and a UUID organization id. OpenAI documents opaque resource
# IDs, not fixed prefixes: accept identifier-safe characters without inventing a prefix or length.
# https://platform.claude.com/docs/en/manage-claude/workload-identity-federation
# https://developers.openai.com/api/docs/guides/workload-identity-federation
# https://cloud.google.com/iam/docs/reference/rest/v1/projects.locations.workloadIdentityPools
_OPAQUE_ID = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_GOOGLE_POOL_ID = r"(?!gcp-)[a-z0-9-]{4,32}"
_ID_PATTERNS = {
    "anthropic": {
        "federation_rule_id": rf"fdrl_{_OPAQUE_ID}",
        "organization_id": r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}",
        "service_account_id": rf"svac_{_OPAQUE_ID}",
        "workspace_id": rf"wrkspc_{_OPAQUE_ID}",
    },
    "google": {
        "project_id": r"[a-z][a-z0-9-]{4,28}[a-z0-9]",
        "project_number": r"[0-9]{1,20}",
        "pool_id": _GOOGLE_POOL_ID,
        "provider_id": _GOOGLE_POOL_ID,
        "location": r"global|[a-z]+-[a-z]+[0-9]+",
    },
}


def validate_provider_federation(config: object) -> dict[str, dict[str, str]]:
    """Return a validated copy, failing closed without echoing values or untrusted field names."""
    if not isinstance(config, dict):
        raise ValueError("sandbox provider federation must be a JSON object")
    if set(config) - PROVIDER_ENVIRONMENT.keys():
        raise ValueError(
            "sandbox provider federation has an unknown provider; allowed providers: "
            + ", ".join(PROVIDER_ENVIRONMENT)
        )

    validated: dict[str, dict[str, str]] = {}
    for provider, fields in PROVIDER_ENVIRONMENT.items():
        if provider not in config:
            continue
        settings = config[provider]
        if not isinstance(settings, dict):
            raise ValueError(f"sandbox provider federation {provider} settings must be an object")
        if set(settings) - fields.keys():
            raise ValueError(f"sandbox provider federation {provider} has unknown fields")
        missing = fields.keys() - settings.keys()
        if missing:
            # These names come from our schema, not from the supplied config.
            raise ValueError(
                f"sandbox provider federation {provider} is missing required fields: "
                + ", ".join(sorted(missing))
            )
        validated[provider] = {}
        for field in fields:
            value = settings[field]
            label = f"sandbox provider federation {provider}.{field}"
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} must be a nonempty identifier string")
            if re.search(r"(?:^|[^a-z0-9])sk-", value, re.IGNORECASE):
                raise ValueError(f"{label} must be a public identifier, not an API key")
            pattern = _ID_PATTERNS.get(provider, {}).get(field, _OPAQUE_ID)
            if not re.fullmatch(pattern, value):
                expected = "a UUID" if field == "organization_id" else "a valid public identifier"
                raise ValueError(f"{label} must be {expected}")
            validated[provider][field] = value
    return validated


def load_provider_federation(
    context_value: object = None, environment_value: str | None = None
) -> dict[str, dict[str, str]]:
    """CDK context wins when present (including ``{}``), then the deployment env, else disabled.

    CLI context and environment values are JSON strings. Native objects from ``cdk.json`` work too.
    Never surface the JSON decoder's exception, which can retain the original secret-bearing input.
    """
    # An unset GitHub Actions repository variable is exported as an empty environment string.
    value = context_value if context_value is not None else (environment_value or None)
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            raise ValueError("sandbox provider federation must contain valid JSON") from None
    return validate_provider_federation(value)


def provider_federation_environment(config: dict[str, dict[str, str]]) -> dict[str, str]:
    """Only public IDs reach the runtime; ``config`` must have been validated first."""
    return {
        environment_name: config[provider][field]
        for provider, fields in PROVIDER_ENVIRONMENT.items()
        if provider in config
        for field, environment_name in fields.items()
    }
