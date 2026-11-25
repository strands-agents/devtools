// Keyless provider clients for Strands TypeScript tests: the twin of strandly_sandbox_auth.py.
// Copy into the project under test so the lazy imports resolve from its node_modules.

const ANTHROPIC_AUDIENCE = 'https://api.anthropic.com'
const OPENAI_AUDIENCE = 'https://api.openai.com/v1'
const GOOGLE_TOKEN_URL = 'https://sts.googleapis.com/v1/token'
const GOOGLE_SUBJECT_TOKEN_TYPE = 'urn:ietf:params:aws:token-type:aws4_request'
const OAUTH_API_BETA = 'oauth-2025-04-20'
// Non-secret: AI SDK providers require a key value; the bearer replaces it per request.
const KEY_PLACEHOLDER = 'workload-identity'
// OpenAI's default buffer exceeds the 300-second bearer, which would refresh on every request.
const OPENAI_REFRESH_BUFFER_SECONDS = 60

function required(name) {
  const value = (process.env[name] ?? '').trim()
  if (!value) throw new Error(`Missing ${name}; configure sandbox provider federation before use`)
  return value
}

function rejectStaticCredentials(...names) {
  for (const name of names) {
    if (process.env[name]) {
      throw new Error(`Unset ${name}; this helper only supports workload identity federation`)
    }
  }
}

function region() {
  const value = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION
  if (!value || value === 'aws-global') {
    throw new Error("Set AWS_REGION to a regional STS endpoint's region (not aws-global)")
  }
  return value
}

function identityTokenProvider(audience) {
  const awsRegion = region()
  return async () => {
    const { STSClient, GetWebIdentityTokenCommand } = await import('@aws-sdk/client-sts')
    // A new client re-resolves rotating credentials; the endpoint ignores AWS_ENDPOINT_URL*.
    const sts = new STSClient({ region: awsRegion, endpoint: `https://sts.${awsRegion}.amazonaws.com` })
    try {
      const { WebIdentityToken } = await sts.send(
        new GetWebIdentityTokenCommand({ Audience: [audience], SigningAlgorithm: 'RS256', DurationSeconds: 300 }),
      )
      if (typeof WebIdentityToken !== 'string' || !WebIdentityToken) {
        throw new Error('AWS STS returned no web identity token')
      }
      return WebIdentityToken
    } finally {
      sts.destroy()
    }
  }
}

function bearerFetch(getToken, { anthropicBeta = false } = {}) {
  return async (url, init = {}) => {
    const headers = new Headers(init.headers)
    headers.delete('x-api-key')
    headers.set('authorization', `Bearer ${await getToken()}`)
    if (anthropicBeta) {
      const flags = (headers.get('anthropic-beta') ?? '').split(',').map((f) => f.trim()).filter(Boolean)
      headers.set('anthropic-beta', [...new Set([...flags, OAUTH_API_BETA])].join(','))
    }
    return fetch(url, { ...init, headers })
  }
}

async function anthropicCredentials() {
  rejectStaticCredentials('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')
  const config = {
    federationRuleId: required('ANTHROPIC_FEDERATION_RULE_ID'),
    organizationId: required('ANTHROPIC_ORGANIZATION_ID'),
    serviceAccountId: required('ANTHROPIC_SERVICE_ACCOUNT_ID'),
    workspaceId: required('ANTHROPIC_WORKSPACE_ID'),
  }
  const { oidcFederationProvider } = await import('@anthropic-ai/sdk/lib/credentials/oidc-federation')
  return oidcFederationProvider({
    ...config,
    identityTokenProvider: identityTokenProvider(ANTHROPIC_AUDIENCE),
    baseURL: ANTHROPIC_AUDIENCE,
    fetch: globalThis.fetch,
  })
}

function openaiWorkloadIdentity() {
  rejectStaticCredentials('OPENAI_API_KEY', 'OPENAI_ADMIN_KEY')
  return {
    identityProviderId: required('OPENAI_IDENTITY_PROVIDER_ID'),
    serviceAccountId: required('OPENAI_SERVICE_ACCOUNT_ID'),
    refreshBufferSeconds: OPENAI_REFRESH_BUFFER_SECONDS,
    provider: { tokenType: 'jwt', getToken: identityTokenProvider(OPENAI_AUDIENCE) },
  }
}

function googleSettings() {
  rejectStaticCredentials('GOOGLE_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_VERTEX_API_KEY')
  return {
    audience:
      `//iam.googleapis.com/projects/${required('GOOGLE_WIF_PROJECT_NUMBER')}` +
      `/locations/global/workloadIdentityPools/${required('GOOGLE_WIF_POOL_ID')}` +
      `/providers/${required('GOOGLE_WIF_PROVIDER_ID')}`,
    project: required('GOOGLE_CLOUD_PROJECT'),
    location: required('GOOGLE_CLOUD_LOCATION'),
  }
}

/** Options for `new Anthropic(...)`; pass the client to Strands `AnthropicModel({ client })`. */
export async function anthropicClientOptions() {
  return { apiKey: null, authToken: null, baseURL: ANTHROPIC_AUDIENCE, credentials: await anthropicCredentials() }
}

/** Options for `new OpenAI(...)`; pass the client to Strands `OpenAIModel({ client })`. */
export function openaiClientOptions() {
  return { apiKey: null, baseURL: OPENAI_AUDIENCE, workloadIdentity: openaiWorkloadIdentity() }
}

/** google-auth-library client for Vertex AI, signing sts:GetCallerIdentity on each refresh. */
export async function googleAuthClient() {
  const { audience } = googleSettings()
  const awsRegion = region()
  const [{ AwsClient }, { fromNodeProviderChain }] = await Promise.all([
    import('google-auth-library'),
    import('@aws-sdk/credential-providers'),
  ])
  const ambient = fromNodeProviderChain()
  return new AwsClient({
    audience,
    subject_token_type: GOOGLE_SUBJECT_TOKEN_TYPE,
    token_url: GOOGLE_TOKEN_URL,
    aws_security_credentials_supplier: {
      getAwsRegion: async () => awsRegion,
      getAwsSecurityCredentials: async () => {
        const { accessKeyId, secretAccessKey, sessionToken } = await ambient()
        return { accessKeyId, secretAccessKey, token: sessionToken }
      },
    },
  })
}

/** Options for `new GoogleGenAI(...)`; pass the client to Strands `GoogleModel({ client })`. */
export async function googleGenAIOptions() {
  const { project, location } = googleSettings()
  return { vertexai: true, project, location, googleAuthOptions: { authClient: await googleAuthClient() } }
}

/** `@ai-sdk/anthropic` provider for Strands `VercelModel`. */
export async function vercelAnthropic() {
  const [{ createAnthropic }, { TokenCache }] = await Promise.all([
    import('@ai-sdk/anthropic'),
    import('@anthropic-ai/sdk/lib/credentials/token-cache'),
  ])
  const cache = new TokenCache(await anthropicCredentials())
  return createAnthropic({
    authToken: KEY_PLACEHOLDER,
    baseURL: `${ANTHROPIC_AUDIENCE}/v1`,
    fetch: bearerFetch(() => cache.getToken(), { anthropicBeta: true }),
  })
}

/** `@ai-sdk/openai` provider for Strands `VercelModel`. */
export async function vercelOpenAI() {
  const [{ createOpenAI }, { WorkloadIdentityAuth }] = await Promise.all([
    import('@ai-sdk/openai'),
    import('openai/auth/workload-identity-auth'),
  ])
  const auth = new WorkloadIdentityAuth(openaiWorkloadIdentity(), globalThis.fetch)
  return createOpenAI({ apiKey: KEY_PLACEHOLDER, baseURL: OPENAI_AUDIENCE, fetch: bearerFetch(() => auth.getToken()) })
}

/** `@ai-sdk/google-vertex` provider for Strands `VercelModel`. */
export async function vercelGoogleVertex() {
  const { project, location } = googleSettings()
  const { createVertex } = await import('@ai-sdk/google-vertex')
  return createVertex({ project, location, googleAuthOptions: { authClient: await googleAuthClient() } })
}
