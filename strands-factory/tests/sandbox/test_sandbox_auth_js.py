"""Offline contract for strandly_sandbox_auth.mjs, run by node against recording fake SDKs.

The fakes only record what the helper hands each SDK; real-SDK behaviour (exchange, caching,
refresh) belongs to the SDKs and is covered for Python in test_provider_federation.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_NODE = shutil.which("node")
_SECRET = "static-key-do-not-echo"
pytestmark = pytest.mark.skipif(_NODE is None, reason="node is not installed")

_ENV = {
    "PATH": "/usr/bin:/bin",
    "AWS_REGION": "us-west-2",
    "AWS_ENDPOINT_URL_STS": "https://untrusted.invalid",
    "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_test",
    "ANTHROPIC_ORGANIZATION_ID": "11111111-2222-4333-8444-555555555555",
    "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_test",
    "ANTHROPIC_WORKSPACE_ID": "wrkspc_test",
    "OPENAI_IDENTITY_PROVIDER_ID": "idp_test",
    "OPENAI_SERVICE_ACCOUNT_ID": "svc_test",
    "GOOGLE_CLOUD_PROJECT": "strandly-sandbox",
    "GOOGLE_WIF_PROJECT_NUMBER": "123456789012",
    "GOOGLE_WIF_POOL_ID": "strandly-sandbox",
    "GOOGLE_WIF_PROVIDER_ID": "aws-sandbox",
    "GOOGLE_CLOUD_LOCATION": "us-central1",
}

_FAKES = {
    "@aws-sdk/client-sts": {
        "index.mjs": """
export const calls = globalThis.__sts = []
export class GetWebIdentityTokenCommand { constructor(input) { this.input = input } }
export class STSClient {
  constructor(config) { this.config = config }
  async send(command) {
    calls.push({ config: this.config, input: command.input, destroyed: false })
    return { WebIdentityToken: `jwt-${calls.length}` }
  }
  destroy() { calls.at(-1).destroyed = true }
}
""",
    },
    "@aws-sdk/credential-providers": {
        "index.mjs": """
export const fromNodeProviderChain = () => async () =>
  ({ accessKeyId: 'ambient-access', secretAccessKey: 'ambient-secret', sessionToken: 'ambient-session' })
""",
    },
    "@anthropic-ai/sdk": {
        "lib/credentials/oidc-federation.mjs": """
export function oidcFederationProvider(config) {
  globalThis.__anthropic = config
  return async () => ({ token: `anthropic-bearer-for-${await config.identityTokenProvider()}`, expiresAt: null })
}
""",
        "lib/credentials/token-cache.mjs": """
export class TokenCache {
  constructor(provider) { this.provider = provider }
  async getToken() { return (await this.provider()).token }
}
""",
    },
    "openai": {
        "auth/workload-identity-auth.mjs": """
export class WorkloadIdentityAuth {
  constructor(config) { this.config = config }
  async getToken() { return `openai-bearer-for-${await this.config.provider.getToken()}` }
}
""",
    },
    "google-auth-library": {
        "index.mjs": "export class AwsClient { constructor(o) { this.options = o } }"
    },
    "@ai-sdk/anthropic": {"index.mjs": "export const createAnthropic = (o) => o"},
    "@ai-sdk/openai": {"index.mjs": "export const createOpenAI = (o) => o"},
    "@ai-sdk/google-vertex": {"index.mjs": "export const createVertex = (o) => o"},
}

_SCRIPT = """
import assert from 'node:assert/strict'
const sent = []
globalThis.fetch = async (url, init) => { sent.push({ url, headers: new Headers(init.headers) }); return 'ok' }
const auth = await import('./strandly_sandbox_auth.mjs')
const out = {}

const anthropic = await auth.anthropicClientOptions()
assert.deepEqual(Object.keys(anthropic).sort(), ['apiKey', 'authToken', 'baseURL', 'credentials'])
assert.equal(anthropic.apiKey, null)
assert.equal((await anthropic.credentials()).token, 'anthropic-bearer-for-jwt-1')
const { identityTokenProvider, fetch, ...anthropicConfig } = globalThis.__anthropic
out.anthropicConfig = anthropicConfig

const openai = auth.openaiClientOptions()
out.openai = { ...openai, workloadIdentity: { ...openai.workloadIdentity, provider: openai.workloadIdentity.provider.tokenType } }
assert.equal(await openai.workloadIdentity.provider.getToken(), 'jwt-2')

const google = await auth.googleGenAIOptions()
const supplier = google.googleAuthOptions.authClient.options.aws_security_credentials_supplier
out.google = { ...google, googleAuthOptions: undefined, auth: { ...google.googleAuthOptions.authClient.options, aws_security_credentials_supplier: undefined } }
out.googleSupplier = [await supplier.getAwsRegion(), await supplier.getAwsSecurityCredentials()]

for (const [name, make] of [['vercelAnthropic', auth.vercelAnthropic], ['vercelOpenAI', auth.vercelOpenAI]]) {
  const provider = await make()
  await provider.fetch('https://example.invalid', { headers: { 'x-api-key': provider.authToken ?? provider.apiKey, 'anthropic-beta': 'files-api' } })
  const { headers } = sent.at(-1)
  out[name] = { baseURL: provider.baseURL, key: provider.authToken ?? provider.apiKey, authorization: headers.get('authorization'), xApiKey: headers.get('x-api-key'), beta: headers.get('anthropic-beta') }
}
out.vertex = { ...(await auth.vercelGoogleVertex()), googleAuthOptions: undefined }
out.sts = globalThis.__sts
console.log(JSON.stringify(out))
"""


@pytest.fixture
def project(tmp_path):
    for package, files in _FAKES.items():
        root = tmp_path / "node_modules" / package
        exports = {f"./{Path(name).with_suffix('')}": f"./{name}" for name in files}
        exports["."] = "./index.mjs"
        (root).mkdir(parents=True)
        (root / "package.json").write_text(json.dumps({"name": package, "exports": exports}))
        for name, source in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(source)
    shutil.copy(_ROOT / "sandbox" / "strandly_sandbox_auth.mjs", tmp_path)
    (tmp_path / "check.mjs").write_text(_SCRIPT)
    return tmp_path


def _run(project, env):
    return subprocess.run(
        [_NODE, "check.mjs"], cwd=project, env=env, capture_output=True, text=True, timeout=60
    )


def test_helper_hands_sdks_federated_config_and_fresh_bearers(project):
    result = _run(project, _ENV)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["anthropicConfig"] == {
        "federationRuleId": "fdrl_test",
        "organizationId": "11111111-2222-4333-8444-555555555555",
        "serviceAccountId": "svac_test",
        "workspaceId": "wrkspc_test",
        "baseURL": "https://api.anthropic.com",
    }
    assert out["openai"] == {
        "apiKey": None,
        "baseURL": "https://api.openai.com/v1",
        "workloadIdentity": {
            "identityProviderId": "idp_test",
            "serviceAccountId": "svc_test",
            "refreshBufferSeconds": 60,
            "provider": "jwt",
        },
    }
    audience = (
        "//iam.googleapis.com/projects/123456789012/locations/global"
        "/workloadIdentityPools/strandly-sandbox/providers/aws-sandbox"
    )
    assert out["google"] == {
        "vertexai": True,
        "project": "strandly-sandbox",
        "location": "us-central1",
        "auth": {
            "audience": audience,
            "subject_token_type": "urn:ietf:params:aws:token-type:aws4_request",
            "token_url": "https://sts.googleapis.com/v1/token",
        },
    }
    assert out["googleSupplier"] == [
        "us-west-2",
        {
            "accessKeyId": "ambient-access",
            "secretAccessKey": "ambient-secret",
            "token": "ambient-session",
        },
    ]
    assert out["vercelAnthropic"] == {
        "baseURL": "https://api.anthropic.com/v1",
        "key": "workload-identity",
        "authorization": "Bearer anthropic-bearer-for-jwt-3",
        "xApiKey": None,
        "beta": "files-api,oauth-2025-04-20",
    }
    assert out["vercelOpenAI"] == {
        "baseURL": "https://api.openai.com/v1",
        "key": "workload-identity",
        "authorization": "Bearer openai-bearer-for-jwt-4",
        "xApiKey": None,
        "beta": "files-api",
    }
    assert out["vertex"] == {"project": "strandly-sandbox", "location": "us-central1"}
    audiences = ["https://api.anthropic.com", "https://api.openai.com/v1"] * 2
    assert [call["input"]["Audience"] for call in out["sts"]] == [[a] for a in audiences]
    for call in out["sts"]:
        assert call["config"] == {
            "region": "us-west-2",
            "endpoint": "https://sts.us-west-2.amazonaws.com",
        }
        assert call["input"]["SigningAlgorithm"] == "RS256"
        assert call["input"]["DurationSeconds"] == 300
        assert call["destroyed"] is True


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("ANTHROPIC_API_KEY", _SECRET, "Unset ANTHROPIC_API_KEY"),
        ("OPENAI_API_KEY", _SECRET, "Unset OPENAI_API_KEY"),
        ("GOOGLE_VERTEX_API_KEY", _SECRET, "Unset GOOGLE_VERTEX_API_KEY"),
        ("ANTHROPIC_WORKSPACE_ID", " ", "Missing ANTHROPIC_WORKSPACE_ID"),
        ("AWS_REGION", "aws-global", "regional STS"),
    ],
)
def test_helper_fails_closed_without_echoing_values(project, name, value, message):
    result = _run(project, {**_ENV, name: value})
    assert result.returncode != 0
    assert message in result.stderr
    assert _SECRET not in result.stderr
