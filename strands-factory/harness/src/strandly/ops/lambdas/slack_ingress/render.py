"""Render a Slack message into prompt-ready text. Pure and standard-library only.

Mentions, channel links, links and user groups become readable text with their ids kept in
parentheses, so the agent can still act on them, and attachments are summarized. Every function is
total: junk shapes yield less output, never an exception. :func:`one_line` flattens untrusted labels
(display names, file names) to one capped line so they cannot read as prompt structure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# ``<@U0123ABC>`` / ``<@W0123|legacy-label>`` — a user mention. W ids are Enterprise Grid users.
_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]{1,20})(?:\|[^>]*)?>")
# ``<#C0123|general>`` — a channel link (the label half is optional).
_CHANNEL_RE = re.compile(r"<#([CGD][A-Z0-9]{1,20})(?:\|([^>]*))?>")
# ``<!subteam^S0123|@platform>`` — a user-group mention.
_SUBTEAM_RE = re.compile(r"<!subteam\^([A-Z0-9]{1,20})(?:\|([^>]*))?>")
# ``<!here>`` / ``<!channel>`` / ``<!everyone>`` — broadcast mentions.
_SPECIAL_RE = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")
# ``<https://x|label>`` / ``<mailto:a@b|a@b>`` — an auto-linked URL.
_LINK_RE = re.compile(r"<((?:https?|mailto|tel):[^|>\s]+)(?:\|([^>]*))?>")

# Cap for one untrusted label (display name, filename) folded into the prompt. Comfortably longer
# than any real name or filename, far too short to hide a forged instruction block in.
MAX_LABEL_CHARS = 80
# Everything a model may read as a line/record boundary: C0 controls (\n, \r, \t, NUL…), DEL, and the
# Unicode line/paragraph separators. Collapsed to a space, never dropped, so words don't fuse.
_LINE_BREAKING_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029\u0085]+")

# How many attachments to name in the prompt before collapsing into "… and N more".
MAX_FILES_LISTED = 5
# How many distinct mentioned users to hand to the (rate-bounded) profile lookup.
MAX_MENTIONS_RESOLVED = 3


def one_line(value: Any, limit: int = MAX_LABEL_CHARS) -> str:
    """One untrusted label -> a single line of at most ``limit`` characters. Total; never raises.

    Applied to every *label* (not the message body) that comes from outside: ``users.info`` display
    names, the secret's ``user_names`` map, attachment filenames and mimetypes. Without it a Slack
    display name — which anyone the sender mentions can choose — could inject newlines and a forged
    ``NEW Slack DM …`` header or ``<system-reminder>`` into the dispatch prompt, or simply push the
    real message out of the way with 50 KB of text.
    """
    if not isinstance(value, str):
        return ""
    # split() also collapses the runs of ordinary whitespace the substitution leaves behind.
    collapsed = " ".join(_LINE_BREAKING_RE.sub(" ", value).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(limit - 1, 0)].rstrip() + "…"


def mentions(text: str, user_id: str) -> bool:
    """Whether ``text`` ``@``-mentions ``user_id`` anywhere (``<@U…>`` or ``<@U…|label>``)."""
    return bool(user_id) and any(m.group(1) == user_id for m in _MENTION_RE.finditer(text or ""))


def mentioned_user_ids(text: str, limit: int = MAX_MENTIONS_RESOLVED) -> list[str]:
    """The distinct user ids ``@``-mentioned in ``text``, in order, capped at ``limit``.

    Feeds :func:`profiles.resolve` so the prompt can name the people the message talks *about*, not
    just the sender. Capped because each unknown id may cost one Slack API call on the request path.
    """
    seen: list[str] = []
    for match in _MENTION_RE.finditer(text or ""):
        user_id = match.group(1)
        if user_id not in seen:
            seen.append(user_id)
            if len(seen) >= limit:
                break
    return seen


def leading_mention(text: str) -> str | None:
    """The user id the text opens with (``<@U…> can you…``), or ``None``."""
    match = _MENTION_RE.match(text.lstrip())
    return match.group(1) if match else None


def _unescape(text: str) -> str:
    """Undo Slack's three HTML escapes. Runs *after* the ``<…>`` rewrites, never before.

    Order matters: a literal ``<`` the user typed reaches us as ``&lt;``, so unescaping first would
    manufacture fake ``<@…>`` tokens out of quoted text. ``&amp;`` goes last so ``&amp;lt;`` (a
    literal ``&lt;``) survives as text instead of collapsing to ``<``.
    """
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _link(url: str, label: str) -> str:
    """``url`` + Slack's optional display label -> one readable token, never a duplicate pair."""
    if not label or label == url:
        return url
    if url.endswith(label):
        # The label is the same target in human form (``<mailto:a@b|a@b>``,
        # ``<https://x.com|x.com>``): keep one copy — the scheme-carrying url, except for
        # mailto/tel where the scheme is noise and the label IS the address.
        return label if url.startswith(("mailto:", "tel:")) else url
    return f"{label} ({url})"


def humanize(text: str, labels: Mapping[str, str] | None = None) -> str:
    """Slack machine text -> readable text: mentions/channels/links resolved, entities unescaped.

    ``labels`` maps a user id to its rendering (``profiles.UserProfile.mention_label()``:
    ``"@Example Maintainer (U0123ABC)"``). An id with no label is left as ``<@U0123ABC>`` — honest about what we
    don't know, and still usable by the agent.
    """
    if not text:
        return ""
    names = labels or {}
    out = _MENTION_RE.sub(lambda m: names.get(m.group(1)) or f"<@{m.group(1)}>", text)
    out = _CHANNEL_RE.sub(lambda m: f"#{m.group(2) or m.group(1)} ({m.group(1)})", out)
    out = _SUBTEAM_RE.sub(lambda m: m.group(2) or f"@{m.group(1)}", out)
    out = _SPECIAL_RE.sub(lambda m: f"@{m.group(1)}", out)
    out = _LINK_RE.sub(lambda m: _link(m.group(1), m.group(2) or ""), out)
    return _unescape(out)


def _human_size(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return ""
    size = float(value)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def describe_files(files: Any, limit: int = MAX_FILES_LISTED) -> list[str]:
    """One human line per attachment: ``"notes.txt (text/plain, 12.0 KB)"``.

    Naming attachments lets the agent respond to what it cannot open (the DM-scoped bot token has
    no ``files:read``).
    """
    if not isinstance(files, (list, tuple)) or not files:
        return []
    lines: list[str] = []
    for entry in files[:limit]:
        if not isinstance(entry, dict):
            continue
        # Filenames are attacker-authored too (the sender picks them, and so does anyone whose
        # file they forward), so they go through the same one-line cap as a display name.
        # Each candidate is flattened *before* the fallback chain picks, so a name that is only
        # line breaks (or a non-string) falls through to the title/id instead of winning empty.
        name = (
            one_line(entry.get("name"))
            or one_line(entry.get("title"))
            or one_line(entry.get("id"))
            or "file"
        )
        kind = one_line(entry.get("mimetype") or entry.get("filetype") or "", 40)
        details = ", ".join(bit for bit in (kind, _human_size(entry.get("size"))) if bit)
        lines.append(f"{name} ({details})" if details else name)
    remaining = len(files) - min(len(files), limit)
    if remaining > 0:
        lines.append(f"... and {remaining} more")
    return lines
