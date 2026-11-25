/**
 * Strandly — the sessions list. Scope (Mine / Portal / Items / All) + a text box that also matches
 * owner logins; rows are borderless cards. Paste a GitHub issue/PR URL (or `owner/repo#n`) into the
 * box and the top row jumps straight to that item's `gh-…` session (Enter does too). "New session"
 * mints a `portal-<login>-…` id; on mobile a composer-first FAB opens a bottom sheet so the first
 * message is typed BEFORE the session exists — the chat page then sends it on mount (router state
 * `firstMessage`). A URL in that draft routes the message to the item's session instead.
 */
import { useEffect, useMemo, useState } from "react";
import { ActionIcon, Badge, Button, Drawer, Group, SegmentedControl, Stack, Text, Textarea, TextInput, Title, UnstyledButton } from "@mantine/core";
import { IconArrowRight, IconChevronRight, IconMessagePlus, IconSearch, IconX } from "@tabler/icons-react";
import { useNavigate } from "react-router-dom";
import { useApi } from "../../api/ApiContext";
import type { SessionSummary } from "../../api/types";
import { EmptyBlock, ErrorBlock, LoadingBlock } from "../../components/StateBlock";
import { useGitHub } from "../../github/GitHubContext";
import { relTime } from "../../lib/format";
import { sessionLink } from "../../lib/links";
import { kindLabel, newPortalSessionId, portalSessionOwner, type ParsedItem } from "../../lib/sessionId";
import { useAsync } from "../../lib/useAsync";
import {
  defaultScope,
  filterSessions,
  itemKindLabel,
  itemTargets,
  pageWindow,
  primaryLabel,
  SCOPES,
  secondaryText,
  SESSIONS_PAGE_SIZE,
  splitItemDraft,
  viewingFor,
  type SessionScope,
} from "./filters";
import type { SessionNavState } from "./navState";
import classes from "./strandly.module.css";

function SessionRow({ session, onOpen }: { session: SessionSummary; onOpen: () => void }) {
  const owner = portalSessionOwner(session.session_id);
  const kind = itemKindLabel(session.session_id);
  const sub = secondaryText(session);
  const status = session.last_status;
  return (
    <UnstyledButton className={classes.row} onClick={onOpen}>
      <div className={classes.rowBody}>
        <div className={classes.rowTop}>
          {status === "running" ? (
            <span className={`${classes.dot} ${classes.dotRunning}`} title="running" />
          ) : status === "failed" ? (
            <span className={`${classes.dot} ${classes.dotFailed}`} title="failed" />
          ) : null}
          <span className={classes.rowTitle}>{primaryLabel(session)}</span>
          {owner ? (
            <Badge size="sm" variant="light" radius="sm" color="frog" style={{ flex: "0 0 auto" }}>
              @{owner}
            </Badge>
          ) : kind ? (
            <Badge size="sm" variant="light" radius="sm" color="gray" style={{ flex: "0 0 auto" }}>
              {kind}
            </Badge>
          ) : null}
        </div>
        {sub ? <div className={classes.rowSub}>{sub}</div> : null}
        <div className={classes.rowMeta}>
          <span>
            {session.runs} run{session.runs === 1 ? "" : "s"}
          </span>
          {session.last_activity ? <span>· {relTime(session.last_activity)}</span> : null}
        </div>
      </div>
      <IconChevronRight size={18} className={classes.chevron} />
    </UnstyledButton>
  );
}

/** The "jump to this item's session" row the search box grows when its text names a GitHub item. */
function JumpRow({ item, onOpen }: { item: ParsedItem; onOpen: () => void }) {
  return (
    <UnstyledButton className={`${classes.row} ${classes.jump}`} onClick={onOpen}>
      <div className={classes.rowBody}>
        <div className={classes.rowTop}>
          <span className={classes.rowTitle}>
            Open Strandly on {item.repo}#{item.number}
          </span>
          <Badge size="sm" variant="light" radius="sm" color="gray" style={{ flex: "0 0 auto" }}>
            {kindLabel(item.kind)}
          </Badge>
        </div>
        <div className={classes.rowSub}>{item.sessionId}</div>
      </div>
      <IconArrowRight size={18} className={classes.chevron} />
    </UnstyledButton>
  );
}

export function SessionsPage() {
  const api = useApi();
  const navigate = useNavigate();
  const { viewer } = useGitHub();
  const login = viewer?.login ?? null;
  const { data, loading, error } = useAsync(() => api.listSessions(), []);

  // Scope: Mine when we know the viewer; the user's own pick sticks once made — unless it was Mine
  // and the token has since gone (no login → no "mine"; fall back to the default).
  const [picked, setPicked] = useState<SessionScope | null>(null);
  const scope = picked && (picked !== "mine" || login) ? picked : defaultScope(login);
  const [query, setQuery] = useState("");
  const [sheetOpen, setSheetOpen] = useState(false);
  const [draft, setDraft] = useState("");

  // Filter the FULL fetched set, then render a bounded window of the result. Client-side paging:
  // GET /api/sessions is one fixed recent window (no cursor), so this bounds the DOM, not the fetch.
  const sessions = useMemo(() => filterSessions(data ?? [], scope, query, login), [data, scope, query, login]);
  const [shown, setShown] = useState(SESSIONS_PAGE_SIZE);
  useEffect(() => setShown(SESSIONS_PAGE_SIZE), [scope, query, login, data]); // a new result set starts at page one
  const visible = useMemo(() => pageWindow(sessions, shown), [sessions, shown]);

  const targets = useMemo(() => itemTargets(query), [query]);
  const openItem = (item: ParsedItem, firstMessage?: string) => {
    const state: SessionNavState = { viewing: viewingFor(item), firstMessage: firstMessage || undefined };
    navigate(sessionLink(item.sessionId), { state });
  };

  const startNew = (firstMessage?: string) => {
    const id = newPortalSessionId(login);
    const state: SessionNavState | undefined = firstMessage ? { firstMessage } : undefined;
    navigate(sessionLink(id), { state });
  };

  const draftItem = useMemo(() => splitItemDraft(draft), [draft]);
  const sendDraft = () => {
    const text = draft.trim();
    if (!text) return;
    setSheetOpen(false);
    setDraft("");
    if (draftItem) openItem(draftItem.item, draftItem.message);
    else startNew(text);
  };

  return (
    <Stack gap="md">
      <Group justify="space-between" wrap="nowrap">
        <Title order={2}>Strandly</Title>
        <Button
          size="sm"
          leftSection={<IconMessagePlus size={16} />}
          onClick={() => startNew()}
          radius="xl"
        >
          New session
        </Button>
      </Group>

      <div className={classes.toolbar}>
        <SegmentedControl
          value={scope}
          onChange={(v) => setPicked(v as SessionScope)}
          data={SCOPES.filter((s) => s.value !== "mine" || login)}
          radius="xl"
          size="sm"
        />
        <TextInput
          className={classes.search}
          value={query}
          onChange={(e) => setQuery(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && targets[0] && !e.nativeEvent.isComposing) {
              e.preventDefault();
              openItem(targets[0]);
            }
          }}
          placeholder="Filter by id, description, @login — or paste a GitHub URL"
          leftSection={<IconSearch size={16} />}
          rightSection={
            query ? (
              <ActionIcon variant="subtle" color="gray" size="sm" onClick={() => setQuery("")} aria-label="Clear">
                <IconX size={14} />
              </ActionIcon>
            ) : null
          }
          radius="xl"
          size="sm"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
        />
      </div>

      {targets.length > 0 ? (
        <Stack gap={6}>
          {targets.map((t) => (
            <JumpRow key={t.sessionId} item={t} onOpen={() => openItem(t)} />
          ))}
        </Stack>
      ) : null}

      {loading ? (
        <LoadingBlock label="Loading sessions…" />
      ) : error ? (
        <ErrorBlock error={error} />
      ) : sessions.length === 0 ? (
        <EmptyBlock
          label={
            targets.length > 0
              ? "No recent session for that item — open it above to start one."
              : query
              ? "No sessions match that filter."
              : scope === "mine"
                ? "No conversations of yours yet — start one."
                : "No sessions yet."
          }
        />
      ) : (
        <Stack gap={6} className={classes.list}>
          {visible.rows.map((s) => (
            <SessionRow key={s.session_id} session={s} onOpen={() => navigate(sessionLink(s.session_id))} />
          ))}
          {visible.remaining > 0 ? (
            <Button
              variant="light"
              color="gray"
              radius="xl"
              h={40}
              mt={4}
              onClick={() => setShown((n) => n + SESSIONS_PAGE_SIZE)}
              style={{ alignSelf: "center" }}
            >
              Show {visible.nextStep} more · {visible.remaining} not shown
            </Button>
          ) : null}
        </Stack>
      )}

      <ActionIcon
        className={classes.fab}
        size={56}
        radius="xl"
        variant="filled"
        aria-label="New conversation"
        onClick={() => setSheetOpen(true)}
      >
        <IconMessagePlus size={24} />
      </ActionIcon>

      <Drawer
        opened={sheetOpen}
        onClose={() => setSheetOpen(false)}
        position="bottom"
        zIndex={300}
        title="New conversation"
        withCloseButton
        trapFocus
        styles={{
          // content-sized sheet with rounded top corners, above the tab bar. Only `height` is
          // overridden: the inner is a ROW flexbox, so Mantine's `flex` IS the width (keep it 100%).
          content: { height: "auto", borderTopLeftRadius: 20, borderTopRightRadius: 20 },
        }}
      >
        <div className={classes.sheetBody}>
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                sendDraft();
              }
            }}
            placeholder="Ask Strandly anything…"
            autosize
            minRows={3}
            maxRows={8}
            data-autofocus
            radius="lg"
            enterKeyHint="send"
          />
          <Group justify="space-between" align="center">
            <Text size="xs" c="dimmed">
              {draftItem
                ? `→ ${draftItem.item.repo}#${draftItem.item.number}'s session`
                : login
                  ? `as @${login}`
                  : "anonymous — add a GitHub token in Settings to own this session"}
            </Text>
            <Button radius="xl" onClick={sendDraft} disabled={!draft.trim()}>
              Send
            </Button>
          </Group>
        </div>
      </Drawer>
    </Stack>
  );
}
