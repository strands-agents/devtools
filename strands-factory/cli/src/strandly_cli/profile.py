"""Client coordinates of one deployment: what a generated CLI bakes in.

They come from the optional ``client`` section of a deployment profile file. Only that section
and ``identity.display_name`` are read; deployment coordinates, secrets and CDK context are not
client coordinates and never enter a generated CLI.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PLACEHOLDER_PREFIX = "REQUIRED_"
CLIENT_KEYS = frozenset(
    {
        "command",
        "display_name",
        "portal_url",
        "api_url",
        "cognito_domain",
        "client_id",
        "region",
        "runtime_arn",
        "default_org",
    }
)
_LOGIN_TRIPLE = ("api_url", "cognito_domain", "client_id")
_COMMAND = re.compile(r"[a-z][a-z0-9-]{0,38}[a-z0-9]")
_REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d")
_RUNTIME_ARN = re.compile(r"arn:aws[a-z-]*:bedrock-agentcore:([a-z0-9-]+):\d{12}:runtime/[A-Za-z0-9_-]+")
_CLIENT_ID = re.compile(r"[A-Za-z0-9]{1,128}")
_ORG = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


class ProfileError(ValueError):
    """A client profile that cannot produce a working CLI (the message names the field)."""


def _check_url(field_name: str, value: str) -> str:
    # Same rule as the ACP bridge: HTTPS, or numeric loopback HTTP for local stubs.
    parts = urlsplit(value)
    loopback = parts.scheme == "http" and parts.hostname in {"127.0.0.1", "::1"}
    if (
        (parts.scheme != "https" and not loopback)
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ProfileError(f"client.{field_name} must be an https:// URL without credentials, query or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class ClientProfile:
    command: str
    display_name: str
    portal_url: str | None = None
    api_url: str | None = None
    cognito_domain: str | None = None
    client_id: str | None = None
    region: str | None = None
    runtime_arn: str | None = None
    default_org: str | None = None

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ProfileError(f"client.{key} must be a nonempty string when present")
            if isinstance(value, str) and value.startswith(PLACEHOLDER_PREFIX):
                raise ProfileError(f"client.{key} is still the placeholder {value!r}; fill it in first")
        if not _COMMAND.fullmatch(self.command):
            raise ProfileError(
                "client.command must be 2-40 lowercase letters, digits and hyphens, starting with a letter"
            )
        triple = [getattr(self, key) for key in _LOGIN_TRIPLE]
        if any(triple) and not all(triple):
            raise ProfileError("client.api_url, client.cognito_domain and client.client_id go together")
        if not self.portal_url and not all(triple):
            raise ProfileError("client needs portal_url, or api_url + cognito_domain + client_id")
        for key in ("portal_url", "api_url", "cognito_domain"):
            value = getattr(self, key)
            if value is not None:
                object.__setattr__(self, key, _check_url(key, value))
        if self.client_id is not None and not _CLIENT_ID.fullmatch(self.client_id):
            raise ProfileError("client.client_id must be a Cognito app-client id (letters and digits)")
        if self.region is not None and not _REGION.fullmatch(self.region):
            raise ProfileError("client.region must be an AWS region such as us-west-2")
        if self.runtime_arn is not None:
            match = _RUNTIME_ARN.fullmatch(self.runtime_arn)
            if not match:
                raise ProfileError("client.runtime_arn must be a bedrock-agentcore runtime ARN")
            if self.region is None:
                object.__setattr__(self, "region", match.group(1))
            elif self.region != match.group(1):
                raise ProfileError("client.region does not match the region in client.runtime_arn")
        if self.default_org is not None and not _ORG.fullmatch(self.default_org):
            raise ProfileError("client.default_org must be a GitHub organization name")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClientProfile:
        unknown = set(data) - CLIENT_KEYS
        if unknown:
            raise ProfileError(f"Unknown client fields: {sorted(unknown)}")
        if "command" not in data or "display_name" not in data:
            raise ProfileError("client.command and client.display_name are required")
        return cls(**data)

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value is not None}

    def auth_file(self) -> Path:
        """This CLI's own login cache, so CLIs for different deployments never share tokens."""
        return Path.home() / ".strandly" / "clients" / self.command / "auth.json"

    def login_hint(self) -> str:
        return f"run `{self.command} login`"


def load_client_profile(path: str | Path, *, command: str | None = None) -> ClientProfile:
    """Read the ``client`` section of a profile file (``--name`` overrides ``client.command``)."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError(f"cannot read profile {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError("a profile file is a JSON object")
    client = raw.get("client")
    if not isinstance(client, dict):
        raise ProfileError(f"{path} has no client section")
    data = dict(client)
    if command:
        data["command"] = command
    if "display_name" not in data:
        identity = raw.get("identity")
        named = identity.get("display_name") if isinstance(identity, dict) else None
        data["display_name"] = named or data.get("command") or "Strandly"
    return ClientProfile.from_dict(data)
