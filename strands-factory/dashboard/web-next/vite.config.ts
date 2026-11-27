/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

import pkg from "./package.json";
import { SHARE_TARGET } from "./src/lib/shareTarget";

// The portal is served as static assets behind CloudFront (its own S3 bucket, see
// infra/stacks/dashboard_stack.py). `base: "./"` keeps asset URLs relative so it works at the CDN
// root or under a sub-path.
//
// PWA: vite-plugin-pwa generates the manifest + a Workbox service worker that precaches the built
// app shell (so "Add to Home screen" on Android gives a standalone, offline-capable app). Two
// things are deliberately NOT cached:
//   - `config.json` — deploy-time wiring (API base, Cognito) that must never be stale;
//   - anything cross-origin — the dashboard API, api.github.com, Cognito. Workbox only handles
//     same-origin precache entries here; there are no runtime caching routes.
export default defineConfig({
  base: "./",
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  plugins: [
    react(),
    VitePWA({
      // "prompt", not "autoUpdate": autoUpdate's registration reloads EVERY open tab the moment a
      // new worker activates (skipWaiting + clientsClaim), which wipes an unsent chat draft in a
      // tab the user is still typing in. The new worker now waits; main.tsx offers a "Reload".
      registerType: "prompt",
      includeAssets: ["icons/*.png"],
      manifest: {
        name: "Strands Maintainer Portal",
        short_name: "Strands",
        description: "Today · Strandly · Issues · PRs — the maintainer's workspace with the Strandly agent.",
        start_url: "./",
        scope: "./",
        display: "standalone",
        orientation: "portrait",
        background_color: "#121215",
        theme_color: "#0c0c0e",
        icons: [
          { src: "icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "icons/icon-512.png", sizes: "512x512", type: "image/png" },
          { src: "icons/maskable-192.png", sizes: "192x192", type: "image/png", purpose: "maskable" },
          { src: "icons/maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
        // Web Share Target: the installed app is a destination in the OS share sheet, so a GitHub
        // issue/PR link shared from Slack / the GitHub app / Chrome opens on its page here. GET onto
        // the app root (no SPA rewrite needed on static hosting; the query sits OUTSIDE the hash
        // route), with namespaced param names so a share launch is unmistakable. The member is
        // defined ONCE in src/lib/shareTarget.ts, which src/lib/shareLaunch.ts also reads at boot.
        // A PWA can't intercept taps on github.com links themselves (that needs github.com to
        // publish assetlinks / an origin association) — see the shareLaunch.ts header.
        share_target: SHARE_TARGET,
        // Links INTO the portal (a `#/pulls/…` URL pasted in Slack) open the installed app rather
        // than a browser tab, and reuse the open window instead of spawning another. navigate-existing
        // reloads that window onto the new URL (an unsent chat draft there is lost); focus-existing +
        // `launchQueue` would avoid the reload but needs its own in-app handler — not worth it yet.
        handle_links: "preferred",
        launch_handler: { client_mode: "navigate-existing" },
      },
      workbox: {
        globPatterns: ["**/*.{js,css,html,png,svg,woff2}"],
        globIgnores: ["**/config.json"],
        navigateFallback: "index.html",
        // Never serve a cached config.json; never touch cross-origin.
        navigateFallbackDenylist: [/config\.json$/],
        maximumFileSizeToCacheInBytes: 4 * 1024 * 1024,
      },
      devOptions: { enabled: false },
    }),
  ],
  server: { port: 5174 },
  // Compile CSS Modules in tests (vitest otherwise stubs them with a proxy) so a test can see the
  // real exported class map — e.g. that third-party classes stay `:global` and unhashed.
  test: { css: { include: [/\.module\.css$/] } },
});
