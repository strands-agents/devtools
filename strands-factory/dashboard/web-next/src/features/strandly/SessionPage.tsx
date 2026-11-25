/**
 * Strandly — one session's chat, immersive: header row · transcript · sticky composer, the whole
 * viewport under the app header (`100dvh` so the mobile keyboard shrinks it instead of hiding the
 * composer). Router state carries `viewing` (the GitHub item the user came from — rides each turn's
 * invocationContext) and `firstMessage` (the composer-first FAB's text — sent once, then stripped
 * from history so a reload or back-navigation can't re-send it).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ActionIcon, Badge, Tooltip } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconArrowLeft, IconCheck, IconLink, IconPlayerStopFilled } from "@tabler/icons-react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useApi } from "../../api/ApiContext";
import { ApiError } from "../../api/errors";
import { AgentChat, type AgentChatHandle } from "../../components/AgentChat";
import { SessionTitle } from "../../components/SessionTitle";
import { ROUTES } from "../../lib/links";
import { commitTitle } from "../../lib/sessionTitle";
import { useBack } from "../../lib/useBack";
import { isPortalSession, portalSessionOwner, sessionLabel } from "../../lib/sessionId";
import { itemKindLabel } from "./filters";
import { readNavState, type SessionNavState } from "./navState";
import classes from "./strandly.module.css";

/** How often the header re-reads the title while a turn runs (the agent may set it mid-turn). */
const TITLE_POLL_MS = 15_000;

/**
 * The session's title for the header: read on mount, every TITLE_POLL_MS while a turn runs and
 * once when it ends; a rename is optimistic and a server read never overwrites a save in flight.
 */
function useSessionTitle(sessionId: string, busy: boolean) {
  const api = useApi();
  const [title, setTitle] = useState<string | null>(null);
  const saving = useRef(0);
  const refresh = useCallback(async () => {
    try {
      const snap = await api.loadSession(sessionId, { limit: 1 });
      if (!saving.current) setTitle(snap.title ?? null);
    } catch {
      // keep what is shown; the next read retries
    }
  }, [api, sessionId]);
  useEffect(() => {
    void refresh();
    if (!busy) return;
    const timer = setInterval(() => void refresh(), TITLE_POLL_MS);
    return () => clearInterval(timer);
  }, [busy, refresh]);
  const rename = async (next: string) => {
    saving.current += 1;
    try {
      await commitTitle({ previous: title, next, apply: setTitle, save: (t) => api.renameSession(sessionId, t) });
    } catch (e) {
      notifications.show({
        color: "red",
        message:
          e instanceof ApiError && e.status === 404
            ? "Send a first message before naming this conversation."
            : "Couldn't rename — the previous title is back.",
      });
    } finally {
      saving.current -= 1;
    }
  };
  return { title, rename };
}

export function SessionPage() {
  const { sessionId = "" } = useParams();
  // Keyed so navigating session → session resets the captured router state (no stale first message).
  return <SessionView key={sessionId} sessionId={sessionId} />;
}

function SessionView({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate();
  const goBack = useBack(ROUTES.strandly);
  const location = useLocation();
  // Captured once: the first message must survive the history rewrite below.
  const [initial] = useState(() => readNavState(location.state));
  const viewing = readNavState(location.state).viewing ?? initial.viewing;
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const chat = useRef<AgentChatHandle>(null);
  const { title, rename } = useSessionTitle(sessionId, busy);

  // Strip `firstMessage` from the history entry: AgentChat sends it once; a reload must not repeat it.
  useEffect(() => {
    if (initial.firstMessage) {
      const kept: SessionNavState | undefined = viewing ? { viewing } : undefined;
      navigate(location.pathname, { replace: true, state: kept });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const owner = portalSessionOwner(sessionId);
  const kind = itemKindLabel(sessionId);
  const label = isPortalSession(sessionId) ? "Conversation" : sessionLabel(sessionId);

  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      notifications.show({ color: "red", message: "Couldn't copy — copy the address bar instead." });
    }
  };

  return (
    <div className={classes.page}>
      <div className={classes.chatHeader}>
        <ActionIcon variant="subtle" color="gray" size={40} aria-label="Back" onClick={goBack}>
          <IconArrowLeft size={20} />
        </ActionIcon>
        <div className={classes.chatTitle}>
          <SessionTitle title={title} fallback={label} hint={sessionId} onRename={(next) => void rename(next)} />
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
        {busy ? (
          <Tooltip label="Stop the running turn" withArrow>
            <ActionIcon variant="light" color="red" size={40} radius="xl" aria-label="Stop" onClick={() => void chat.current?.stop()}>
              <IconPlayerStopFilled size={16} />
            </ActionIcon>
          </Tooltip>
        ) : null}
        <Tooltip label={copied ? "Copied" : "Copy link"} withArrow>
          <ActionIcon variant="subtle" color="gray" size={40} aria-label="Copy link to this session" onClick={() => void copyLink()}>
            {copied ? <IconCheck size={18} /> : <IconLink size={18} />}
          </ActionIcon>
        </Tooltip>
      </div>
      <AgentChat
        ref={chat}
        sessionId={sessionId}
        layout="fill"
        initialMessage={initial.firstMessage}
        onBusyChange={setBusy}
        invocationContext={{ surface: "maintainer-portal", view: "chat", viewing }}
      />
    </div>
  );
}
