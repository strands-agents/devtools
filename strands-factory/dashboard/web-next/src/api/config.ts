import { configurePortalIdentity, type PortalIdentity } from "../lib/team";

/**
 * Runtime + build-time configuration.
 *
 * Two layers:
 *   1. Build-time env (Vite `import.meta.env.VITE_*`) — used in dev.
 *   2. Runtime `/config.json` fetched next to index.html — written by CDK at deploy time, so a
 *      single built bundle can point at different backends without a rebuild.
 * Runtime config wins when present; env is the dev fallback and the default.
 */

export type DataSource = "mock" | "live";

export interface RuntimeConfig {
  dataSource: DataSource;
  apiBase: string;
  // Cognito Hosted-UI + PKCE fields (see auth/cognito.ts). Auth engages ONLY when BOTH cognitoDomain
  // and cognitoClientId are present; when absent (local dev / mock) the app is login-free. redirectUri
  // is optional and defaults to `${origin}/`. Written into the runtime config.json at deploy time.
  cognitoDomain?: string;
  cognitoClientId?: string;
  redirectUri?: string;
  portalIdentity?: PortalIdentity;
}

const ENV_DATA_SOURCE = (import.meta.env.VITE_DATA_SOURCE as DataSource | undefined) ?? "mock";
const ENV_API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
const ENV_COGNITO_DOMAIN = (import.meta.env.VITE_COGNITO_DOMAIN as string | undefined) ?? "";
const ENV_COGNITO_CLIENT_ID = (import.meta.env.VITE_COGNITO_CLIENT_ID as string | undefined) ?? "";
const ENV_REDIRECT_URI = (import.meta.env.VITE_REDIRECT_URI as string | undefined) ?? "";

let cached: RuntimeConfig | null = null;

/** Load config once (runtime /config.json over env defaults). Never throws — falls back to mock. */
export async function loadConfig(): Promise<RuntimeConfig> {
  if (cached) return cached;
  const mockIdentity: PortalIdentity = { agentLogin: "example-agent", githubOrgs: ["example-org"], maintainersOrg: "example-org", maintainersTeamSlug: "maintainers" };
  const base: RuntimeConfig = {
    dataSource: ENV_DATA_SOURCE,
    portalIdentity: ENV_DATA_SOURCE === "mock" ? mockIdentity : {},
    apiBase: ENV_API_BASE,
    cognitoDomain: ENV_COGNITO_DOMAIN || undefined,
    cognitoClientId: ENV_COGNITO_CLIENT_ID || undefined,
    redirectUri: ENV_REDIRECT_URI || undefined,
  };
  try {
    const r = await fetch("config.json", { cache: "no-store" });
    if (r.ok) {
      const j = (await r.json()) as Partial<RuntimeConfig>;
      // A live config.json with a real dataSource/apiBase overrides the env default. An empty
      // scaffold (apiBase: "") leaves the env default in place so dev keeps working.
      cached = {
        dataSource: (j.dataSource as DataSource) || base.dataSource,
        apiBase: j.apiBase || base.apiBase,
        // Cognito: a real config.json value wins; an empty scaffold ("") falls back to the env/dev
        // value (usually unset → auth stays disabled, keeping local/mock login-free).
        cognitoDomain: j.cognitoDomain || base.cognitoDomain,
        cognitoClientId: j.cognitoClientId || base.cognitoClientId,
        redirectUri: j.redirectUri || base.redirectUri,
        portalIdentity: j.portalIdentity ?? (((j.dataSource as DataSource) || base.dataSource) === "mock" ? mockIdentity : {}),
      };
      configurePortalIdentity(cached.portalIdentity);
      return cached;
    }
  } catch {
    // No config.json in dev — fine, use env.
  }
  cached = base;
  configurePortalIdentity(base.portalIdentity);
  return cached;
}
