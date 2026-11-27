"""Operator-selected identity and deployment configuration (no secret values).

Profiles are local JSON files, never selected by model input. GitHub logins, including
user accounts, are independent of AWS accounts and resource names. An installation
with no profile has no repository write or mention-invocation authority.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The longest explicit suffix is _sandbox_managed_sagemaker_role (30 chars, IAM max 64).
# A 32-character combined stem also leaves room for Memory/CI (48) and account-scoped S3 (63).
MAX_DEPLOYMENT_STEM = 32
# Derived values append these words to a stem (``-infra`` tag, ``-managed`` prefix, ``_sandbox_``
# roles), so as an environment or a later name segment they would alias another pair's scopes.
RESERVED_COORDINATE_WORDS = frozenset({"infra", "managed", "sandbox"})


def validate_deployment_coordinates(name: str, environment: str) -> None:
    """Resource names and authorization scopes derive from ``{name}-{environment}``.

    Only ``name`` may contain hyphens, so the last hyphen always separates the two and
    distinct accepted pairs never derive the same stem; no reserved word follows a hyphen, so
    no stem is another stem plus a derived suffix.
    """
    if len(f"{name}_{environment}") > MAX_DEPLOYMENT_STEM:
        raise ValueError("deployment name and environment together must fit a 32-character stem, including separator")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", name):
        raise ValueError("deployment.name must be a lowercase AWS-safe name: letters, digits and hyphens, starting with a letter")
    if not re.fullmatch(r"[a-z][a-z0-9]{0,23}", environment):
        raise ValueError(
            "deployment.environment must be lowercase letters and digits, starting with a letter; "
            "hyphens are reserved for deployment.name"
        )
    reserved = sorted(RESERVED_COORDINATE_WORDS & {environment, *name.split("-")[1:]})
    if reserved:
        raise ValueError(
            f"{', '.join(reserved)} cannot be deployment.environment or a deployment.name segment "
            "after the first: derived resource names and scopes already append those words"
        )


@dataclass(frozen=True)
class Identity:
    display_name: str = "Strandly"
    github_login: str = ""
    mention_handle: str = ""
    git_author_name: str = "Strandly"
    git_author_email: str = "strandly@localhost"
    managed_repositories: tuple[str, ...] = ()
    allowed_authors: tuple[str, ...] = ()
    allowed_orgs: tuple[str, ...] = ()
    github_orgs: tuple[str, ...] = ()
    maintainers_org: str = ""
    maintainers_team_slug: str = ""


@dataclass(frozen=True)
class Deployment:
    name: str
    environment: str
    account: str
    region: str
    secret_references: dict[str, str]

    def __post_init__(self) -> None:
        validate_deployment_coordinates(self.name, self.environment)
        if not re.fullmatch(r"\d{12}", self.account):
            raise ValueError("deployment.account must be an explicit 12-digit AWS account")
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", self.region):
            raise ValueError("deployment.region is required")
        if "config" not in self.secret_references:
            raise ValueError("deployment.secret_references.config is required (ARN or secret name)")
        if not all(isinstance(v, str) and v for v in self.secret_references.values()):
            raise ValueError("secret_references must contain nonempty references, never values")


@dataclass(frozen=True)
class Profile:
    identity: Identity = field(default_factory=Identity)
    deployment: Deployment | None = None
    skill_packs: tuple[str, ...] = ()
    deployment_extensions: tuple[str, ...] = ()
    # Trusted CDK composition inputs, not runtime/model-editable options.
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Profile:
        raw = json.loads(Path(path).read_text())
        # ``client`` holds the coordinates a generated CLI bakes in; only strandly_cli reads it.
        unknown = set(raw) - {"identity", "deployment", "skill_packs", "deployment_extensions", "context", "client"}
        if unknown:
            raise ValueError(f"Unknown profile fields: {sorted(unknown)}")
        identity = dict(raw.get("identity", {}))
        for key in ("managed_repositories", "allowed_authors", "allowed_orgs", "github_orgs"):
            value = identity.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise ValueError(f"identity.{key} must be a list of nonempty strings")
            identity[key] = tuple(value)
        return cls(
            identity=Identity(**identity),
            deployment=Deployment(**raw["deployment"]) if "deployment" in raw else None,
            skill_packs=tuple(raw.get("skill_packs", ())),
            deployment_extensions=tuple(raw.get("deployment_extensions", ())),
            context=raw.get("context", {}),
        )

    def runtime_values(self) -> dict[str, str]:
        i = self.identity
        values = {
            "STRANDLY_DISPLAY_NAME": i.display_name,
            "STRANDLY_GITHUB_LOGIN": i.github_login,
            "STRANDLY_MENTION_HANDLE": i.mention_handle,
            "STRANDLY_GIT_AUTHOR_NAME": i.git_author_name,
            "STRANDLY_GIT_AUTHOR_EMAIL": i.git_author_email,
            "STRANDLY_ALLOWED_OWNERS": ",".join(i.managed_repositories),
            "STRANDLY_MENTION_ALLOWED_AUTHORS": ",".join(i.allowed_authors),
            "STRANDLY_MENTION_ALLOWED_ORGS": ",".join(i.allowed_orgs),
            "STRANDLY_SKILL_PACKS": ",".join(self.skill_packs),
        }
        if self.deployment is not None:
            legacy = self.context.get("legacy_scope")
            legacy_enabled = legacy is not None and str(legacy).strip() != "" and str(legacy).lower() not in ("false", "0", "no")
            tag = "strandly" if legacy_enabled else f"{self.deployment.name}-{self.deployment.environment}"
            values["STRANDLY_TEST_MANAGED_BY_VALUE"] = tag
            values["STRANDLY_TEST_NAME_PREFIX"] = f"{tag}-managed"
            values["STRANDLY_PROTECTED_MANAGED_BY_VALUE"] = f"{tag}-infra"
            values["AWS_REGION"] = self.deployment.region
            values["STRANDLY_SECRETS_ARN"] = self.deployment.secret_references["config"]
        return values

    def deployment_context(self) -> dict[str, Any]:
        if self.deployment is None:
            raise ValueError("Deployment requires explicit name, environment, account, region and secret_references")
        d = self.deployment
        # Resource coordinates cannot be overridden by context or inferred from GitHub identity.
        return {
            **self.context, "name": d.name, "env": d.environment,
            "account": d.account, "region": d.region,
            "secret_arn": d.secret_references["config"],
            "mention_handle": self.identity.mention_handle,
            "portal_identity": {"agentLogin": self.identity.github_login, "githubOrgs": list(self.identity.github_orgs), "maintainersOrg": self.identity.maintainers_org, "maintainersTeamSlug": self.identity.maintainers_team_slug},
            "profile_coordinates": {"name": d.name, "env": d.environment, "account": d.account, "region": d.region},
        }
