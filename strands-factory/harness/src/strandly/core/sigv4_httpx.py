"""SigV4-signs outbound httpx requests — for MCP endpoints behind AWS_IAM auth (AgentCore Gateway).

The Gateway's web-search connector uses ``AWS_IAM`` authorizer type: "authorized to call this
Gateway" is a SigV4-signed request from a role the Gateway trusts, not a bearer token. The
Streamable-HTTP MCP transport (``strands.tools.mcp._compat.streamable_http_transport``, which
wraps either ``mcp`` major) accepts an ``httpx.Auth`` via its ``auth=`` kwarg — this signs each
request with the process's ambient AWS
credentials (the runtime's execution role; ``AWS_PROFILE`` locally) instead of an
``Authorization: Bearer`` header.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session


class SigV4HttpxAuth(httpx.Auth):
    """Signs each request with SigV4 using boto3's ambient credential chain.

    ``requires_request_body = True``: SigV4 hashes the request body into the signature, so httpx
    must buffer it before ``auth_flow`` runs (fine for MCP's small JSON-RPC payloads).
    """

    requires_request_body = True

    def __init__(self, service: str, region: str) -> None:
        self._service = service
        self._region = region
        self._session = Session()

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, Any, None]:
        credentials = self._session.get_credentials()
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=dict(request.headers),
        )
        SigV4Auth(credentials, self._service, self._region).add_auth(aws_request)
        request.headers.update(dict(aws_request.headers))
        yield request
