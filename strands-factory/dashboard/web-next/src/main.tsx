import React from "react";
import ReactDOM from "react-dom/client";
import { Button, MantineProvider, v8CssVariablesResolver } from "@mantine/core";
import { Notifications, notifications } from "@mantine/notifications";
import { HashRouter } from "react-router-dom";
import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "./styles.css";
import { registerSW } from "virtual:pwa-register";
import { theme } from "./theme";
import { ApiProvider } from "./api/ApiContext";
import { AuthProvider } from "./auth/AuthContext";
import { GitHubProvider } from "./github/GitHubContext";
import { App } from "./App";
import { consumeShareLaunch } from "./lib/shareLaunch";

// Service worker: precaches the app shell for installability/offline; config.json and every API
// call are excluded (see vite.config.ts). A new build never reloads a tab on its own — it waits
// until the user taps Reload here (or every portal tab is closed), so an unsent draft survives.
const updateSW = registerSW({
  onNeedRefresh() {
    notifications.show({
      id: "pwa-update",
      autoClose: false,
      withCloseButton: true,
      title: "Update available",
      message: (
        <Button size="xs" radius="xl" mt={6} onClick={() => void updateSW(true)}>
          Reload to update
        </Button>
      ),
    });
  },
});

// Share-target launch (`?share_url=…` from the OS share sheet): resolve to the item's route and
// rewrite the URL BEFORE the HashRouter mounts, so it starts on that page. Must run before render.
const shareLaunch = consumeShareLaunch();

// HashRouter (not BrowserRouter): the portal is served as static assets behind CloudFront with no
// SPA rewrite rule wired for this app yet, and the current dashboard already routes on the hash —
// so deep links survive a hard refresh without server config.
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {/* v8CssVariablesResolver: keep the translucent `light` variant colors Mantine 9 replaced with
        solid ones (the portal uses variant="light" for its badges and tinted buttons). */}
    <MantineProvider theme={theme} defaultColorScheme="dark" cssVariablesResolver={v8CssVariablesResolver}>
      <Notifications position="top-right" />
      <HashRouter>
        <ApiProvider>
          <AuthProvider>
            <GitHubProvider>
              <App />
            </GitHubProvider>
          </AuthProvider>
        </ApiProvider>
      </HashRouter>
    </MantineProvider>
  </React.StrictMode>,
);

// Shared text that wasn't a GitHub issue / PR / discussion link: say so instead of landing on the
// Strandly tab in silence. Safe to call before <Notifications> has mounted: Mantine's store buffers
// the entry and the host picks it up when it subscribes (its mount effect spreads the current state).
if (shareLaunch && !shareLaunch.item) {
  const preview = shareLaunch.shared.length > 80 ? `${shareLaunch.shared.slice(0, 77)}…` : shareLaunch.shared;
  notifications.show({
    id: "share-unrecognized",
    title: "Not a GitHub issue or PR link",
    message: preview ? `Shared: ${preview}` : "Share a github.com issue, pull request or discussion link to open it here.",
    color: "yellow",
    autoClose: 8000,
  });
}
