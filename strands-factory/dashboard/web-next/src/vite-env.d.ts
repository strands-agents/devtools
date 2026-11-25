/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

interface ImportMetaEnv {
  readonly VITE_DATA_SOURCE?: "mock" | "live";
  readonly VITE_API_BASE?: string;
  readonly VITE_COGNITO_DOMAIN?: string;
  readonly VITE_COGNITO_CLIENT_ID?: string;
  readonly VITE_REDIRECT_URI?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/** Injected at build time from package.json (vite.config.ts `define`). */
declare const __APP_VERSION__: string;
