/**
 * A session title that renames in place: click (or Enter/Space on focus) to edit, Enter or blur
 * saves, Escape cancels. The parent owns the value and the save (see lib/sessionTitle
 * `commitTitle` for the optimistic update and its rollback).
 */
import { useRef, useState } from "react";
import { TextInput, UnstyledButton } from "@mantine/core";
import { IconPencil } from "@tabler/icons-react";
import { SESSION_TITLE_MAX_LEN, titleEditKey } from "../lib/sessionTitle";
import classes from "./SessionTitle.module.css";

export interface SessionTitleProps {
  /** The stored title, or null when the session has none. */
  title: string | null;
  /** What to show when there is no title (today's derived label). */
  fallback: string;
  /** Hover text for the label (the raw session id). */
  hint?: string;
  /** Save a new title ("" clears it). Errors are the caller's to report; the editor just closes. */
  onRename: (next: string) => void;
}

export function SessionTitle({ title, fallback, hint, onRename }: SessionTitleProps) {
  const [draft, setDraft] = useState<string | null>(null);
  // Set once a key or blur has decided the edit, so the blur that follows a keyed close is a no-op.
  const settled = useRef(false);

  const open = () => {
    settled.current = false;
    setDraft(title ?? "");
  };
  const close = (save: boolean) => {
    if (settled.current || draft === null) return;
    settled.current = true;
    const next = draft;
    setDraft(null);
    if (save) onRename(next);
  };

  if (draft !== null) {
    return (
      <TextInput
        className={classes.input}
        size="xs"
        value={draft}
        maxLength={SESSION_TITLE_MAX_LEN}
        placeholder={fallback}
        aria-label="Conversation title"
        autoFocus
        onFocus={(e) => e.currentTarget.select()}
        onChange={(e) => setDraft(e.currentTarget.value)}
        onKeyDown={(e) => {
          const action = titleEditKey(e.key, e.nativeEvent.isComposing);
          if (!action) return;
          e.preventDefault();
          close(action === "save");
        }}
        onBlur={() => close(true)}
      />
    );
  }
  return (
    <UnstyledButton className={classes.label} onClick={open} title={hint} aria-label={`Rename conversation: ${title ?? fallback}`}>
      <span className={classes.text}>{title ?? fallback}</span>
      <IconPencil size={14} className={classes.pencil} aria-hidden />
    </UnstyledButton>
  );
}
