/**
 * The manifest `share_target` member — ONE definition, read by both sides of the share launch:
 * vite.config.ts emits it into `manifest.webmanifest`, and `shareLaunch.ts` reads the same param
 * names back out of `location.search` at boot. It lives in its own DOM-free module because
 * vite.config.ts is type-checked by tsconfig.node.json (no DOM lib) and must not pull in the
 * browser-side resolver. See shareLaunch.ts for the why/how of the whole feature.
 *
 * `action: "./"` is the app root — the same relative URL as the manifest's `start_url` / `scope`
 * (so it is inside scope, as the spec requires) and the one URL static hosting serves `index.html`
 * for without a rewrite rule. GET keeps it a plain navigation: `/?share_url=…&share_text=…`.
 */
export const SHARE_PARAMS = { url: "share_url", text: "share_text", title: "share_title" } as const;

export const SHARE_TARGET = {
  action: "./",
  method: "GET",
  params: SHARE_PARAMS,
} as const;
