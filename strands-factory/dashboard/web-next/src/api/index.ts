import type { PortalApi } from "./types";
import { loadConfig } from "./config";
import { MockApi } from "./mock";
import { LiveApi } from "./client";
import { configureCognito } from "../auth/cognito";

let instance: PortalApi | null = null;
let dataSource: "mock" | "live" = "mock";
// True when the app WANTED live data but had to fall back to mock (empty apiBase). Distinct from a
// deliberate mock run, because a live deploy silently serving fabricated data + a mock identity is a
// misconfiguration the operator must see, not a demo.
let fallback = false;

export interface ApiInit {
  api: PortalApi;
  dataSource: "mock" | "live";
  fallback: boolean;
}

/** Build (once) the PortalApi implementation the config selects. */
export async function initApi(): Promise<ApiInit> {
  if (instance) return { api: instance, dataSource, fallback };
  const cfg = await loadConfig();
  dataSource = cfg.dataSource;
  // Arm the dashboard-API auth engine from runtime config. Cognito engages ONLY when both a domain
  // and a client id are present; otherwise it stays dormant (no bearer, no redirect) so local dev
  // and mock mode remain login-free. Redirect URI falls back to `${origin}/` inside cognito.ts.
  configureCognito(
    cfg.cognitoDomain && cfg.cognitoClientId
      ? { domain: cfg.cognitoDomain, clientId: cfg.cognitoClientId, redirectUri: cfg.redirectUri ?? "" }
      : null,
  );
  // Live requires an apiBase; if it's missing we fall back to mock so the app still runs.
  if (cfg.dataSource === "live" && cfg.apiBase) {
    instance = new LiveApi(cfg.apiBase);
  } else {
    if (cfg.dataSource === "live") {
      console.warn("dataSource=live but apiBase is empty — falling back to mock data.");
      dataSource = "mock";
      fallback = true;
    }
    instance = new MockApi();
  }
  return { api: instance, dataSource, fallback };
}

export type { PortalApi } from "./types";
