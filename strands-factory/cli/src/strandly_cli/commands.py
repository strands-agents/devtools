"""The client commands, shared by ``strandly`` and every generated CLI.

``strandly`` (no baked profile) resolves login coordinates from flags, the environment and the
shared cache ``~/.strandly/auth.json`` exactly as before. A generated CLI carries a
:class:`ClientProfile`: its coordinates are fixed, no flag or environment variable overrides
them, and it keeps its own token cache.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strandly_cli import __version__
from strandly_cli.profile import ClientProfile, ProfileError, load_client_profile

MCP_LABEL_ENV = "STRANDLY_MCP_DEPLOYMENT_LABEL"
MCP_REPOSITORIES_ENV = "STRANDLY_MCP_REPOSITORIES"


@dataclass(frozen=True)
class ClientContext:
    """Which CLI is running: ``profile`` is ``None`` for ``strandly`` itself."""

    prog: str = "strandly"
    profile: ClientProfile | None = None

    @property
    def auth_file(self) -> Path | None:
        return self.profile.auth_file() if self.profile else None

    @property
    def login_command(self) -> str:
        return f"{self.prog} login"


# ---- parsers ---------------------------------------------------------------------------------


def add_whoami_parser(sub: Any) -> None:
    sub.add_parser("whoami", help="show the cached login, who you are, and the person the API maps you to")


def add_identity_parser(sub: Any) -> None:
    p = sub.add_parser(
        "identity",
        help="show or set who you are (an advisory GitHub login sent with every request)",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--github", metavar="LOGIN", help="set the GitHub login by hand (not verified)")
    group.add_argument(
        "--verify",
        action="store_true",
        help="read the login from GitHub with `gh auth token` or $GITHUB_TOKEN (the token stays local)",
    )
    group.add_argument("--clear", action="store_true", help="forget the identity")


def add_sessions_parser(sub: Any) -> None:
    p = sub.add_parser("sessions", help="list recent sessions on the deployment, or show one")
    p.add_argument("session_id", nargs="?", help="show this session's transcript (clipped) instead of the list")
    p.add_argument("--limit", type=int, default=20, help="rows to list (default 20)")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    p.add_argument("--tools", action="store_true", help="with a session id: include tool calls")


def add_mcp_parser(sub: Any) -> None:
    p = sub.add_parser("mcp", help="MCP server over stdio that delegates to the deployed agent (uses the login)")
    p.add_argument(
        "--agentcore",
        action="store_true",
        help="call InvokeAgentRuntime directly with ambient AWS credentials (needs boto3) instead of the dashboard API",
    )
    p.add_argument("--runtime-arn", help="--agentcore: the runtime ARN (else the baked or configured one)")
    p.add_argument("--region", help="--agentcore: AWS region (else the baked or configured one)")


def add_acp_parser(sub: Any, prog: str) -> None:
    sub.add_parser("acp", help=f"remote ACP agent over stdio (uses `{prog} login`)")


def add_chat_parser(sub: Any) -> Any:
    p = sub.add_parser("chat", help="chat with the deployed agent (a REPL, or one turn with --prompt)")
    p.add_argument("--session-id", "-s", help="join this session (default: a new one)")
    p.add_argument("--prompt", "-p", help="send one message, print the answer and exit")
    return p


def add_build_parser(sub: Any) -> None:
    p = sub.add_parser("cli", help="build a deployment-specific client CLI")
    cli_sub = p.add_subparsers(dest="cli_command", required=True)
    b = cli_sub.add_parser("build", help="build an installable CLI wheel from a profile's client section")
    b.add_argument("--profile", required=True, help="profile JSON file with a client section")
    b.add_argument("--name", help="command name (default: client.command)")
    b.add_argument("--out", default="dist", help="output directory (default: dist)")


# ---- handlers --------------------------------------------------------------------------------


def _err(ctx: ClientContext, command: str, message: str) -> int:
    print(f"{command}: {message}", file=sys.stderr)
    return 1


def _load_login(ctx: ClientContext, command: str) -> dict[str, Any] | None:
    from strandly_mcp.auth import load_auth

    auth = load_auth(ctx.auth_file)
    if auth is None:
        _err(ctx, command, f"not logged in — run `{ctx.login_command}` first")
    return auth


def run_login(ctx: ClientContext) -> int:
    """Generated CLIs only: ``strandly login`` keeps its flag-driven path in the harness CLI."""
    from strandly_mcp.auth import login_command

    p = ctx.profile
    if p is None:
        raise RuntimeError("run_login needs a baked profile")
    if p.api_url:
        code = login_command(
            api_url=p.api_url,
            cognito_domain=p.cognito_domain,
            client_id=p.client_id,
            path=ctx.auth_file,
            default_portal_url=None,
        )
    else:
        code = login_command(portal_url=p.portal_url, path=ctx.auth_file, default_portal_url=None)
    if code == 0:
        verify_identity_after_login(ctx)
    return code


def verify_identity_after_login(ctx: ClientContext) -> None:
    """After a sign-in, verify who you are with a local GitHub token; never fails the login."""
    from strandly_mcp import identity

    path = identity.identity_path(ctx.auth_file)
    try:
        record = identity.verify(path)
    except identity.IdentityError as exc:
        current = identity.load_identity(path)
        print(f"identity: {exc}", file=sys.stderr)
        print(f"identity: {identity.describe(current)}", file=sys.stderr)
        return
    print(f"identity: you are {identity.describe(record)}", file=sys.stderr)


def run_identity(ctx: ClientContext, args: argparse.Namespace) -> int:
    from strandly_mcp import identity

    path = identity.identity_path(ctx.auth_file)
    try:
        if args.clear:
            identity.clear_identity(path)
            print("you: not set")
            return 0
        if args.github:
            login = args.github.strip().removeprefix("@")
            record = identity.save_identity(login, identity.VERIFIED_MANUAL, path)
            print(
                f"identity: @{login} is unverified; `{ctx.prog} identity --verify` checks it with GitHub",
                file=sys.stderr,
            )
            print(f"you: {identity.describe(record)}")
            return 0
        if args.verify:
            record = identity.verify(path)
            print(f"you: {identity.describe(record)}")
            return 0
    except identity.IdentityError as exc:
        return _err(ctx, "identity", str(exc))
    current = identity.load_identity(path)
    print(f"you: {identity.describe(current)}")
    if current is None:
        print(f"set it with `{ctx.prog} identity --verify` or `{ctx.prog} identity --github <login>`", file=sys.stderr)
    return 0


def _person_line(ctx: ClientContext, auth: dict[str, Any]) -> str:
    """The people-directory row the API maps this client's identity to (best effort)."""
    try:
        seen = _client(ctx, auth).whoami()
    except Exception:  # noqa: BLE001 - whoami must print the local state even when the API is down
        return "(the API is unreachable)"
    if seen is None:
        return "(this deployment does not report it)"
    person = seen.get("person")
    if not isinstance(person, dict) or not person.get("person_id"):
        return "not in the people directory"
    label = str(person.get("name") or person["person_id"])
    if person.get("role"):
        label += f", {person['role']}"
    return f"{label} ({person['person_id']})"


def run_logout(ctx: ClientContext) -> int:
    from strandly_mcp.auth import logout_command

    return logout_command(path=ctx.auth_file)


def run_whoami(ctx: ClientContext) -> int:
    from strandly_mcp import identity
    from strandly_mcp.auth import auth_path

    auth = _load_login(ctx, "whoami")
    if auth is None:
        return 1
    you = identity.load_identity(identity.identity_path(ctx.auth_file))
    expires = auth.get("expires_at")
    left = int(expires - time.time()) if isinstance(expires, (int, float)) else None
    lines = [
        ("deployment", ctx.profile.display_name if ctx.profile else "(from the login)"),
        ("api", auth.get("api_url") or "?"),
        ("cognito", auth.get("cognito_domain") or "?"),
        ("client id", auth.get("client_id") or "?"),
        (
            "access token",
            "absent" if left is None else (f"valid for {left // 60} min" if left > 0 else "expired (refreshes on use)"),
        ),
        ("cache", str(ctx.auth_file or auth_path())),
        ("you", identity.describe(you)),
    ]
    if you is not None:
        lines.append(("person", _person_line(ctx, auth)))
    for key, value in lines:
        print(f"{key:>12}: {value}")
    return 0


def _client(ctx: ClientContext, auth: dict[str, Any]) -> Any:
    from strandly_mcp.dashboard import DashboardClient

    return DashboardClient(auth, auth_file=ctx.auth_file)


def run_sessions(ctx: ClientContext, args: argparse.Namespace) -> int:
    from strandly_mcp.auth import AuthError
    from strandly_mcp.dashboard import DashboardApiError, clip_session, summarize_sessions

    auth = _load_login(ctx, "sessions")
    if auth is None:
        return 1
    client = _client(ctx, auth)
    try:
        if args.session_id:
            detail = client.session(args.session_id)
            if detail is None:
                return _err(ctx, "sessions", f"no session {args.session_id!r}")
            print(json.dumps(clip_session(detail, include_tools=args.tools), indent=2, ensure_ascii=False))
            return 0
        rows = summarize_sessions(client.sessions(), args.limit)
    except (AuthError, DashboardApiError) as exc:
        return _err(ctx, "sessions", str(exc))
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("no sessions")
        return 0
    for row in rows:
        # A title (set in the UI or by the agent) names the session better than its first prompt.
        description = " ".join(str(row.get("title") or row.get("description") or "").split())[:60]
        print(f"{row.get('session')}\t{row.get('last_status') or '-'}\t{row.get('last_activity') or '-'}\t{description}")
    return 0


def run_chat(ctx: ClientContext, args: argparse.Namespace) -> int:
    from strandly_cli.chat import chat_remote

    auth = _load_login(ctx, "chat")
    if auth is None:
        return 1
    return chat_remote(
        _client(ctx, auth),
        session_id=args.session_id,
        prompt=args.prompt,
        display_name=ctx.profile.display_name if ctx.profile else "Strandly",
    )


def run_acp(ctx: ClientContext) -> int:
    try:
        from strandly_cli import acp
    except ImportError:
        hint = "install strands-factory[acp]" if ctx.profile is None else "reinstall this CLI with its dependencies"
        return _err(ctx, "acp", f"ACP needs agent-client-protocol and httpx: {hint}")
    return acp.main(ctx.auth_file, ctx.login_command)


def run_mcp(ctx: ClientContext, args: argparse.Namespace) -> int:
    # An INFO "Processing request of type …" per call would land on stderr for every tool call.
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)
    p = ctx.profile
    if p is not None:
        os.environ.setdefault(MCP_LABEL_ENV, p.display_name)
        if p.default_org:
            os.environ.setdefault(MCP_REPOSITORIES_ENV, f"{p.default_org}/*")
    if args.agentcore:
        from strandly_mcp import agentcore

        if p is not None:
            runtime_arn, region = args.runtime_arn or p.runtime_arn, args.region or p.region
        else:
            runtime_arn = agentcore.resolve_runtime_arn(args.runtime_arn)
            region = agentcore.resolve_region(args.region)
        if not runtime_arn:
            return _err(ctx, "mcp --agentcore", "no runtime ARN (pass --runtime-arn)")
        if not region:
            return _err(ctx, "mcp --agentcore", "no region (pass --region)")
        try:
            import boto3  # noqa: F401 — fail before the first tool call, naming the missing package
        except ImportError:
            return _err(ctx, "mcp --agentcore", str(agentcore.AwsExtraMissingError()))
        from strandly_mcp.server import serve_mcp_proxy

        serve_mcp_proxy(runtime_arn, region)
        return 0
    auth = _load_login(ctx, "mcp")
    if auth is None:
        return 1
    from strandly_mcp.server import build_dashboard_proxy_server

    build_dashboard_proxy_server(_client(ctx, auth)).run()
    return 0


def run_build(args: argparse.Namespace) -> int:
    from strandly_cli.build import build_cli

    try:
        profile = load_client_profile(args.profile, command=args.name)
        wheel = build_cli(profile, Path(args.out))
    except (ProfileError, OSError) as exc:
        print(f"cli build: {exc}", file=sys.stderr)
        return 1
    print(wheel)
    print(f"install: pipx install {wheel}", file=sys.stderr)
    return 0


# ---- a generated CLI's entrypoint ------------------------------------------------------------


def build_parser(profile: ClientProfile) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=profile.command,
        description=f"{profile.display_name}: use the deployed agent from a terminal, an ACP client or an MCP client.",
    )
    parser.add_argument("--version", action="version", version=f"{profile.command} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help=f"browser sign-in to {profile.display_name}")
    sub.add_parser("logout", help="drop (and best-effort revoke) the cached login")
    add_whoami_parser(sub)
    add_identity_parser(sub)
    add_chat_parser(sub)
    add_sessions_parser(sub)
    add_acp_parser(sub, profile.command)
    add_mcp_parser(sub)
    return parser


def main(argv: list[str] | None = None, *, profile: ClientProfile) -> int:
    import strandly_mcp.auth as mcp_auth

    args = build_parser(profile).parse_args(argv)
    # Messages raised deep in the shared transport name this CLI's login command.
    mcp_auth.LOGIN_HINT = profile.login_hint()
    ctx = ClientContext(prog=profile.command, profile=profile)
    handlers = {
        "login": lambda: run_login(ctx),
        "logout": lambda: run_logout(ctx),
        "whoami": lambda: run_whoami(ctx),
        "identity": lambda: run_identity(ctx, args),
        "chat": lambda: run_chat(ctx, args),
        "sessions": lambda: run_sessions(ctx, args),
        "acp": lambda: run_acp(ctx),
        "mcp": lambda: run_mcp(ctx, args),
    }
    return handlers[args.command]()
