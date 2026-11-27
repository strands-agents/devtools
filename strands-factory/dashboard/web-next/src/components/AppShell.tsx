/**
 * AppShell — the tab chrome: Today · Strandly · Issues · PRs · Schedules · Settings.
 *
 * Desktop (≥ sm): a slim left rail with icon + label. Mobile (< sm): the rail is hidden and a fixed
 * bottom tab bar (safe-area aware, blurred) takes over — the PWA-on-Android shape. The header is
 * minimal: frog + title on the left, the live/mock + sign-in state and who you are (the advisory
 * GitHub login, src/lib/person.ts) on the right. Everything else
 * (token, scope, context, appearance) lives on the Settings page, not in the header.
 */
import { useSyncExternalStore, type ReactNode } from "react";
import { Alert, AppShell as MantineAppShell, Badge, Button, Group, Tooltip } from "@mantine/core";
import {
  IconAlertTriangle,
  IconCircleDot,
  IconClockHour4,
  IconGitPullRequest,
  IconInbox,
  IconLogin,
  IconMessageCircle,
  IconSettings,
  IconUserQuestion,
} from "@tabler/icons-react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { useDataSource, useFallback } from "../api/ApiContext";
import { useAuth } from "../auth/AuthContext";
import { ROUTES } from "../lib/links";
import { getPersonLogin, subscribePersonLogin } from "../lib/person";
import classes from "./AppShell.module.css";

export const NAV = [
  { to: ROUTES.today, label: "Today", icon: IconInbox },
  { to: ROUTES.strandly, label: "Strandly", icon: IconMessageCircle },
  { to: ROUTES.issues, label: "Issues", icon: IconCircleDot },
  { to: ROUTES.pulls, label: "PRs", icon: IconGitPullRequest },
  { to: ROUTES.schedules, label: "Schedules", icon: IconClockHour4 },
  { to: ROUTES.settings, label: "Settings", icon: IconSettings },
] as const;

/** The nav item the current path belongs to (longest matching prefix). */
export function activeNavTo(pathname: string): string | undefined {
  return NAV.map((n) => n.to as string)
    .filter((to) => pathname === to || pathname.startsWith(to + "/"))
    .sort((a, b) => b.length - a.length)[0];
}

/** Routes that want the full viewport (no padded column) — the chat. */
function isImmersive(pathname: string): boolean {
  return pathname.startsWith(`${ROUTES.strandly}/`);
}

export function FrogLogo({ className }: { className?: string }) {
  return <img src="./icons/frog.png" alt="" className={className} draggable={false} />;
}

/** Who the portal says you are: `@login` (links to Settings), or a prompt to set it. */
export function PersonBadge({ login }: { login: string }) {
  if (login) {
    return (
      <Tooltip label="Sent with every request as X-Strandly-Person. Change it in Settings.">
        <Badge component={Link} to={ROUTES.settings} variant="light" color="gray" radius="sm" style={{ cursor: "pointer", textTransform: "none" }}>
          @{login}
        </Badge>
      </Tooltip>
    );
  }
  return (
    <Button component={Link} to={ROUTES.settings} size="compact-xs" variant="subtle" leftSection={<IconUserQuestion size={14} />}>
      Who are you?
    </Button>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const dataSource = useDataSource();
  const fallback = useFallback();
  const { enabled: authEnabled, authenticated, login } = useAuth();
  const location = useLocation();
  const activeTo = activeNavTo(location.pathname);
  const immersive = isImmersive(location.pathname);
  // Cognito is on but the user hasn't signed in: API-backed views would 401 — prompt once, up top.
  const needsSignIn = authEnabled && !authenticated;
  const personLogin = useSyncExternalStore(subscribePersonLogin, getPersonLogin, () => "");

  return (
    <MantineAppShell
      header={{ height: "var(--portal-header-h)" }}
      navbar={{ width: 200, breakpoint: "sm", collapsed: { mobile: true } }}
      padding={immersive ? 0 : "md"}
    >
      <MantineAppShell.Header withBorder={false}>
        <div className={classes.header}>
          <Link to={ROUTES.today} className={classes.brand} aria-label="Strands Maintainer Portal">
            <FrogLogo className={classes.logo} />
            <span className={classes.title}>Maintainer Portal</span>
          </Link>
          <Group gap="xs">
            {dataSource === "mock" ? (
              <Tooltip label="Serving fake data (no backend)">
                <Badge color="yellow" variant="light" radius="sm">
                  demo
                </Badge>
              </Tooltip>
            ) : null}
            {needsSignIn ? (
              <Button size="compact-xs" variant="light" leftSection={<IconLogin size={14} />} onClick={login}>
                Sign in
              </Button>
            ) : null}
            <PersonBadge login={personLogin} />
          </Group>
        </div>
      </MantineAppShell.Header>

      <MantineAppShell.Navbar withBorder={false} className={classes.rail}>
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={`${classes.railItem} ${activeTo === item.to ? classes.railItemActive : ""}`}
            aria-current={activeTo === item.to ? "page" : undefined}
          >
            <item.icon size={20} stroke={1.75} />
            {item.label}
          </NavLink>
        ))}
      </MantineAppShell.Navbar>

      <MantineAppShell.Main>
        <div className={immersive ? undefined : classes.main}>
          {dataSource === "mock" && fallback && !immersive ? (
            <Alert
              icon={<IconAlertTriangle size={16} />}
              color="red"
              variant="light"
              mb="md"
              title="Misconfigured: falling back to fake data"
            >
              Requested the live backend but no API base is configured — fix the deploy config (apiBase).
            </Alert>
          ) : null}
          {children}
        </div>
      </MantineAppShell.Main>

      <nav className={classes.bottomNav} aria-label="Primary" style={{ gridTemplateColumns: `repeat(${NAV.length}, 1fr)` }}>
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={`${classes.tab} ${activeTo === item.to ? classes.tabActive : ""}`}
            aria-current={activeTo === item.to ? "page" : undefined}
          >
            <item.icon size={22} stroke={1.75} />
            {item.label}
          </NavLink>
        ))}
      </nav>
    </MantineAppShell>
  );
}
