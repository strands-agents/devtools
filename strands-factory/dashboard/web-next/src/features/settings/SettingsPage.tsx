/**
 * Settings — one page, six sections, no modals:
 *
 *   1. GitHub token    (browser-only PAT: save / validate / clear + scopes + XSS caveat)
 *   2. Who are you     (the advisory GitHub login sent as X-Strandly-Person, src/lib/person.ts)
 *   3. Strandly        (the standing "Strandly context" sent with every chat turn)
 *   4. Scope           (orgs scanned, maintainers team) — focus views are gone
 *   5. Appearance      (dark / light / system) + Install-as-app
 *   6. Account         (Cognito sign-in/out for the dashboard API; data source; build)
 *
 * The token section keeps the HARD RULE from the old TokenSettings modal: the GitHub token lives in
 * localStorage only and is never sent to the dashboard API (src/github/token.ts is its only store).
 */
import { useEffect, useState, useSyncExternalStore } from "react";
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Group,
  List,
  PasswordInput,
  SegmentedControl,
  Stack,
  TagsInput,
  Text,
  Textarea,
  TextInput,
  Title,
  useMantineColorScheme,
} from "@mantine/core";
import {
  IconAlertTriangle,
  IconBrandGithub,
  IconCheck,
  IconDeviceMobile,
  IconLogin,
  IconLogout,
  IconRefresh,
  IconTrash,
} from "@tabler/icons-react";
import { useDataSource } from "../../api/ApiContext";
import { useAuth } from "../../auth/AuthContext";
import { useGitHub } from "../../github/GitHubContext";
import { DEFAULT_SCOPE, getScope, resetScope, setScope, subscribeScope } from "../../lib/scope";
import { getPersonLogin, normalizeLogin, setPersonLogin, subscribePersonLogin } from "../../lib/person";
import {
  getUserContext,
  setUserContext,
  subscribeUserContext,
  USER_CONTEXT_MAX_CHARS,
} from "../../lib/userContext";
import { Section } from "../../components/Section";
import { useInstallPrompt } from "../../lib/useInstallPrompt";

export function SettingsPage() {
  return (
    <Stack gap="lg">
      <Title order={2}>Settings</Title>
      <TokenSection />
      <PersonSection />
      <StrandlyContextSection />
      <ScopeSection />
      <AppearanceSection />
      <AccountSection />
    </Stack>
  );
}

// --- 1. GitHub token --------------------------------------------------------------------------

function TokenSection() {
  const { viewer, validating, tokenError, saveToken, clearToken, isMock, hasToken } = useGitHub();
  const [draft, setDraft] = useState("");

  function handleSave() {
    if (!draft.trim()) return;
    saveToken(draft.trim());
    setDraft("");
  }

  return (
    <Section
      icon={<IconBrandGithub size={18} />}
      title="GitHub token"
      description="Stored in this browser only — never sent to the Strandly server. Every GitHub call goes browser → api.github.com."
    >
      {viewer && !isMock ? (
        <Alert color="frog" variant="light" icon={<IconCheck size={16} />}>
          Signed in as <b>{viewer.name}</b> (<Code>@{viewer.login}</Code>).
        </Alert>
      ) : tokenError && !isMock ? (
        <Alert color="red" variant="light" icon={<IconAlertTriangle size={16} />} title="Token rejected">
          {tokenError} — check the value and scopes, then save again.
        </Alert>
      ) : isMock ? (
        <Alert color="yellow" variant="light">
          Demo mode (fake identity {viewer?.name}). Add your token to load <b>real</b> GitHub data right now.
        </Alert>
      ) : (
        <Alert color="blue" variant="light">
          No token saved — the portal is read-only until you add one.
        </Alert>
      )}

      <PasswordInput
        label="Personal access token"
        placeholder="github_pat_… or ghp_…"
        value={draft}
        onChange={(e) => setDraft(e.currentTarget.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") handleSave();
        }}
      />
      <Group>
        <Button onClick={handleSave} loading={validating} disabled={!draft.trim()} leftSection={<IconCheck size={16} />}>
          Save & validate
        </Button>
        {hasToken && !isMock ? (
          <Button variant="subtle" color="red" onClick={clearToken} leftSection={<IconTrash size={16} />}>
            Clear token
          </Button>
        ) : null}
      </Group>

      <div>
        <Group gap="xs" mb={4}>
          <Text size="sm" fw={600}>
            Recommended scopes
          </Text>
          <Badge size="xs" variant="light">
            fine-grained PAT
          </Badge>
        </Group>
        <Text size="sm" c="dimmed" mb={6}>
          Use a{" "}
          <Anchor href="https://github.com/settings/personal-access-tokens/new" target="_blank">
            fine-grained token
          </Anchor>{" "}
          for a single resource owner and only the selected repositories you need. A fine-grained PAT
          cannot span multiple resource owners; match the organizations below to its access. Use the
          narrowest permissions that let you review and triage:
        </Text>
        <List size="sm" spacing={2} c="dimmed">
          <List.Item>
            <b>Pull requests</b> — read & write (diffs, reviews, comments as yourself)
          </List.Item>
          <List.Item>
            <b>Issues</b> — read & write (triage, comments, close and reopen)
          </List.Item>
          <List.Item>
            <b>Metadata</b> — read (search)
          </List.Item>
          <List.Item>
            <b>Organization → Members</b> — read (the maintainers team; optional)
          </List.Item>
        </List>
        <Text size="xs" c="dimmed" mt={6}>
          The token sits in <Code>localStorage</Code>, so an XSS bug on this origin could read it — keep scopes
          minimal and clear it on shared machines.
        </Text>
      </div>
    </Section>
  );
}

// --- 2. Who are you ------------------------------------------------------------------------------

export function PersonSection() {
  const saved = useSyncExternalStore(subscribePersonLogin, getPersonLogin, () => "");
  const { viewer } = useGitHub();
  const [draft, setDraft] = useState(saved);
  useEffect(() => setDraft(saved), [saved]);
  const login = normalizeLogin(draft);
  const invalid = draft.trim() !== "" && !login;
  const verified = Boolean(saved && viewer && viewer.login.toLowerCase() === saved.toLowerCase());

  return (
    <Section
      title="Who are you?"
      description="Your GitHub login, sent with every request. Strandly knows who you are from your sign-in, which an operator binds to your people-directory row; this login is advisory and never changes that."
    >
      {saved ? (
        <Group gap="xs">
          <Text size="sm">
            You are <b>@{saved}</b>
          </Text>
          <Badge color={verified ? "green" : "gray"} variant="light" radius="sm">
            {verified ? "matches your GitHub token" : "set by hand"}
          </Badge>
        </Group>
      ) : null}
      <TextInput
        label="GitHub login"
        placeholder="octocat"
        value={draft}
        error={invalid ? "Not a GitHub login" : undefined}
        onChange={(e) => setDraft(e.currentTarget.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && login) setPersonLogin(login);
        }}
      />
      <Group gap="xs">
        <Button size="compact-sm" disabled={!login || login === saved} onClick={() => setPersonLogin(login)}>
          Save
        </Button>
        {viewer && viewer.login !== saved ? (
          <Button
            size="compact-sm"
            variant="light"
            leftSection={<IconBrandGithub size={14} />}
            onClick={() => setPersonLogin(viewer.login)}
          >
            Use my GitHub login (@{viewer.login})
          </Button>
        ) : null}
        {saved ? (
          <Button variant="subtle" color="red" size="compact-sm" onClick={() => setPersonLogin("")}>
            Clear
          </Button>
        ) : null}
      </Group>
    </Section>
  );
}

// --- 3. Strandly context ---------------------------------------------------------------------

function StrandlyContextSection() {
  const saved = useSyncExternalStore(subscribeUserContext, getUserContext, () => "");
  const [draft, setDraft] = useState(saved);
  useEffect(() => setDraft(saved), [saved]);
  const dirty = draft.trim() !== saved;

  return (
    <Section
      title="Strandly context"
      description="Sent with every message you send Strandly (as ambient context, not an instruction). Tell it who you are and how you like to work."
    >
      <Textarea
        autosize
        minRows={3}
        maxRows={10}
        maxLength={USER_CONTEXT_MAX_CHARS}
        placeholder="e.g. I'm a maintainer on sdk-python (model providers + tools). Prefer short answers with links. Timezone CET."
        value={draft}
        onChange={(e) => setDraft(e.currentTarget.value)}
      />
      <Group justify="space-between">
        <Text size="xs" c="dimmed" className="tabular">
          {draft.length}/{USER_CONTEXT_MAX_CHARS}
        </Text>
        <Group gap="xs">
          {saved ? (
            <Button variant="subtle" color="red" size="compact-sm" onClick={() => setUserContext("")}>
              Clear
            </Button>
          ) : null}
          <Button size="compact-sm" disabled={!dirty} onClick={() => setUserContext(draft)}>
            Save
          </Button>
        </Group>
      </Group>
    </Section>
  );
}

// --- 4. Scope -----------------------------------------------------------------------------------

function ScopeSection() {
  const scope = useSyncExternalStore(subscribeScope, getScope, getScope);
  const [orgs, setOrgs] = useState<string[]>(scope.orgs);
  const [mOrg, setMOrg] = useState(scope.maintainersOrg);
  const [mSlug, setMSlug] = useState(scope.maintainersTeamSlug);
  useEffect(() => {
    setOrgs(scope.orgs);
    setMOrg(scope.maintainersOrg);
    setMSlug(scope.maintainersTeamSlug);
  }, [scope]);
  const dirty =
    orgs.join(",") !== scope.orgs.join(",") || mOrg !== scope.maintainersOrg || mSlug !== scope.maintainersTeamSlug;

  return (
    <Section
      title="Scope"
      description="Which GitHub orgs the Today / Issues / PRs tabs search, and the team whose members count as maintainers. Per-browser; takes effect immediately."
    >
      <TagsInput
        label="Organizations"
        value={orgs}
        onChange={setOrgs}
        placeholder="add an org and press Enter"
        splitChars={[",", " "]}
      />
      <Group grow>
        <TextInput label="Maintainers org" value={mOrg} onChange={(e) => setMOrg(e.currentTarget.value)} />
        <TextInput label="Team slug" value={mSlug} onChange={(e) => setMSlug(e.currentTarget.value)} />
      </Group>
      <Group justify="space-between">
        <Button variant="subtle" size="compact-sm" leftSection={<IconRefresh size={14} />} onClick={() => resetScope()}>
          Reset to defaults ({DEFAULT_SCOPE.orgs.join(", ")})
        </Button>
        <Button
          size="compact-sm"
          disabled={!dirty || orgs.length === 0}
          onClick={() => setScope({ orgs, maintainersOrg: mOrg, maintainersTeamSlug: mSlug })}
        >
          Save
        </Button>
      </Group>
    </Section>
  );
}

// --- 5. Appearance + install --------------------------------------------------------------------

function AppearanceSection() {
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  const { canInstall, install, installed } = useInstallPrompt();
  return (
    <Section
      title="Appearance"
      description="Dark by default. The app is installable — on Android, Chrome's menu → “Add to Home screen”. Once installed, it appears in Android's share sheet: share a GitHub issue or PR link from Slack, the GitHub app or Chrome → Strands opens it."
    >
      <SegmentedControl
        value={colorScheme}
        onChange={(v) => setColorScheme(v as "dark" | "light" | "auto")}
        data={[
          { label: "Dark", value: "dark" },
          { label: "Light", value: "light" },
          { label: "System", value: "auto" },
        ]}
      />
      {installed ? (
        <Text size="sm" c="dimmed">
          Running as an installed app.
        </Text>
      ) : canInstall ? (
        <Button variant="light" leftSection={<IconDeviceMobile size={16} />} onClick={install} w="fit-content">
          Install as app
        </Button>
      ) : null}
    </Section>
  );
}

// --- 6. Account ---------------------------------------------------------------------------------

function AccountSection() {
  const { enabled, authenticated, identity, login, logout } = useAuth();
  const dataSource = useDataSource();
  return (
    <Section title="Account" description="Sign-in for the Strandly API (sessions + chat). Separate from your GitHub token.">
      {enabled ? (
        authenticated ? (
          <Group justify="space-between">
            <Text size="sm">
              Signed in{identity?.email ? ` as ${identity.email}` : identity?.username ? ` as ${identity.username}` : ""}.
            </Text>
            <Button variant="subtle" color="red" size="compact-sm" leftSection={<IconLogout size={14} />} onClick={logout}>
              Sign out
            </Button>
          </Group>
        ) : (
          <Button leftSection={<IconLogin size={16} />} onClick={login} w="fit-content">
            Sign in
          </Button>
        )
      ) : (
        <Text size="sm" c="dimmed">
          Sign-in is not configured for this deployment (local / demo).
        </Text>
      )}
      <Group gap="xs">
        <Text size="xs" c="dimmed">
          Data source
        </Text>
        <Badge size="xs" variant="light" color={dataSource === "mock" ? "yellow" : "frog"}>
          {dataSource}
        </Badge>
        <Text size="xs" c="dimmed">
          · build {__APP_VERSION__}
        </Text>
      </Group>
    </Section>
  );
}
