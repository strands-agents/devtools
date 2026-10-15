"""Command-line entrypoint: ``strandly``.

Local agent:
- ``run "<prompt>"``           — one-shot, stream to the terminal.
- ``chat``                     — interactive REPL (``--agentcore`` streams against the deployed runtime).
- ``serve {agentcore,mcp}``    — start a serving surface locally.
- ``brief``                    — produce the team morning brief.

Deployed AgentCore Runtime (the full lifecycle, one CLI):
- ``provision``                — create the Secrets Manager secret + AgentCore backends.
- ``deploy``                   — deploy to a hosted runtime (drives the agentcore toolkit).
- ``invoke "<prompt>"``        — fire-and-forget a long task (works from any folder; no GitHub context needed).
- ``poll <taskId>``            — poll a fire-and-forget run (read back from AgentCore Memory).

Config is loaded from AWS Secrets Manager (``STRANDLY_SECRETS_ARN``) or a local ``.env``; the
surface is the subcommand. ``run``/``chat`` are human-in-the-loop by default. ``invoke``/``poll``
resolve the deployed runtime from ``--runtime-arn`` / ``$STRANDLY_RUNTIME_ARN`` / the record
``deploy`` wrote to ``~/.strandly/runtime.json`` / a local ``.bedrock_agentcore.yaml`` — so they
work from any directory.
"""

from __future__ import annotations

import argparse
import logging
import sys


def _quiet_noisy_loggers() -> None:
    """Silence library log noise on the interactive surfaces.

    - ``mcp.server.lowlevel`` emits an INFO ``Processing request of type ...`` per call.
    - ``SystemPromptSkills`` warns ``previously injected skills block not found`` on the first
      model call of each turn: we build a fresh agent per turn (stateless serving) while the
      session manager rehydrates the plugin's ``last_injected`` state, so its strip-then-reinject
      can't find the marker in the freshly composed prompt. Skills still inject correctly — the
      warning is expected for our per-turn-fresh-agent pattern, so we drop it to ERROR.
    """
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)
    logging.getLogger("strandly_harness.plugins.system_prompt_skills").setLevel(logging.ERROR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strandly", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run a single prompt and exit")
    p_run.add_argument("prompt", help="the prompt to run")
    p_run.add_argument("--hitl", action="store_true", help="approve/interrupt each tool call")
    p_run.add_argument(
        "--session-id", "-s", default="strandly-run",
        help="session id scoping conversation persistence",
    )

    p_chat = sub.add_parser("chat", help="interactive REPL (local, or --agentcore for the deployed runtime)")
    p_chat.add_argument("--hitl", action="store_true", help="approve/interrupt each tool call")
    p_chat.add_argument(
        "--agentcore",
        action="store_true",
        help="stream against the deployed AgentCore runtime instead of a local agent "
        "(needs a resolvable runtime ARN + AGENTCORE_MEMORY_ID)",
    )
    p_chat.add_argument(
        "--session-id", "-s", default="strandly-chat",
        help="session id scoping conversation persistence",
    )

    sub.add_parser("serve", help="serve the agent").add_argument(
        "mode", choices=["agentcore", "mcp"], help="serving surface"
    )

    p_prov = sub.add_parser(
        "provision", help="create the secret + AgentCore backends (drives the infra/ CDK app)"
    )
    p_prov.add_argument("--name", default="strandly", help="resource name prefix")
    p_prov.add_argument(
        "--env", default="dev", help="environment suffix isolating this deployment (dev/prod/…)"
    )
    p_prov.add_argument("--region", help="AWS region (else ambient)")
    p_prov.add_argument("--account", help="AWS account id (else resolved from credentials)")
    p_prov.add_argument(
        "--no-kb",
        action="store_true",
        help="skip the long-term-memory Bedrock KB (provisioned by default)",
    )
    p_prov.add_argument(
        "--github-token",
        metavar="TOKEN",
        help=(
            "fold a GitHub token into the secret (enables the use_github tool for the deployed "
            'agent). Pass it explicitly, e.g. --github-token="$STRANDLY_GITHUB_TOKEN", so you '
            "control exactly which token is stored."
        ),
    )

    p_port = sub.add_parser("port", help="translate a feature across languages")
    p_port.add_argument(
        "--issue", required=True, help="GitHub issue URL for the translation ([PORT] issue)"
    )
    p_port.add_argument(
        "--pr",
        help="GitHub PR URL of existing work to iterate on (omit for a new translation)",
    )
    p_port.add_argument("--hitl", action="store_true", help="approve/interrupt each tool call")

    p_brief = sub.add_parser("brief", help="produce the team morning brief")
    p_brief.add_argument(
        "--since", default="24h", help="lookback window for activity/news (e.g. 24h, 3d)"
    )
    p_brief.add_argument(
        "--out",
        default="./briefs",
        help="output file or directory (default ./briefs/, one dated file per day)",
    )
    p_brief.add_argument(
        "--session-id",
        "-s",
        help="session id scoping conversation persistence. Default: strandly-brief-<UTC date>, "
        "so each day is a fresh thread but a same-day rerun threads the same conversation.",
    )

    p_deploy = sub.add_parser(
        "deploy", help="deploy to a hosted AgentCore Runtime (drives the agentcore toolkit)"
    )
    p_deploy.add_argument("--name", default="strandly", help="runtime name")
    p_deploy.add_argument("--region", help="AWS region (else AWS_REGION / ambient)")
    p_deploy.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="runtime env var (repeatable) — e.g. AGENTCORE_MEMORY_ID=…, STRANDLY_SECRETS_ARN=…",
    )
    p_deploy.add_argument(
        "--no-observability",
        action="store_true",
        help="don't set AGENT_OBSERVABILITY_ENABLED (skip GenAI trace emission; on by default)",
    )

    p_invoke = sub.add_parser(
        "invoke", help="invoke the deployed runtime (works from any folder, unlike agentcore invoke)"
    )
    p_invoke.add_argument("prompt", help="the prompt / task for the deployed agent")
    p_invoke.add_argument(
        "--session-id",
        "-s",
        help="runtime session id (affinity; 33-256 chars). Default: the canonical GitHub-item id "
        "derived from $GITHUB_CONTEXT (gh-<owner>-<repo>-<kind>-<n>), else an ephemeral per-run id.",
    )
    p_invoke.add_argument("--runtime-arn", help="deployed runtime ARN (else resolved automatically)")
    p_invoke.add_argument("--region", help="AWS region (else resolved automatically)")

    p_poll = sub.add_parser("poll", help="poll a deployed run for its status/result")
    p_poll.add_argument("task_id", help="the taskId returned when the run was launched")
    p_poll.add_argument(
        "--session-id", "-s", help="the run's session id (affinity). Default: same derivation as invoke."
    )
    p_poll.add_argument("--runtime-arn", help="deployed runtime ARN (else resolved automatically)")
    p_poll.add_argument("--region", help="AWS region (else resolved automatically)")

    return parser


def _parse_env(pairs: list[str]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` strings from repeated --env flags."""
    out: dict[str, str] = {}
    for p in pairs:
        key, sep, value = p.partition("=")
        if not sep:
            raise SystemExit(f"--env expects KEY=VALUE, got: {p!r}")
        out[key.strip()] = value
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _quiet_noisy_loggers()

    if args.command == "provision":
        from strandly_harness.serve.provisioning import provision

        extra_secrets: dict[str, str] = {}
        if args.github_token:
            # Store under STRANDLY_GITHUB_TOKEN — the key the deployed agent's github gate reads.
            extra_secrets["STRANDLY_GITHUB_TOKEN"] = args.github_token

        provision(
            name=args.name,
            env=args.env,
            region=args.region,
            account=args.account,
            with_kb=not args.no_kb,
            extra_secrets=extra_secrets or None,
        )
        return 0

    if args.command == "deploy":
        from strandly_harness.serve.deploy import deploy as run_deploy
        from strandly_harness.serve.deploy import resolve_region

        region = resolve_region(args.region)
        if not region:
            print("deploy: no region (pass --region or set AWS_REGION)", file=sys.stderr)
            return 1
        return run_deploy(
            name=args.name,
            region=region,
            env=_parse_env(args.env),
            observability=not args.no_observability,
        )

    if args.command in ("invoke", "poll"):
        import json

        from strandly_harness.serve.deploy import resolve_region, resolve_runtime_arn

        runtime_arn = resolve_runtime_arn(args.runtime_arn)
        region = resolve_region(args.region)
        if not runtime_arn:
            print(
                f"{args.command}: no runtime ARN — deploy first, pass --runtime-arn, or set "
                "STRANDLY_RUNTIME_ARN",
                file=sys.stderr,
            )
            return 1
        if not region:
            print(f"{args.command}: no region (pass --region or set AWS_REGION)", file=sys.stderr)
            return 1

        # One scoped id for every ingress: an explicit --session-id wins, else derive the
        # canonical GitHub-item id from $GITHUB_CONTEXT so an Action invoke and a mention land in
        # the same session (strandly_harness.ops.lambdas.mention_poller.sessions).
        from strandly_harness.ops.lambdas.mention_poller.sessions import (
            session_id_from_github_event,
        )

        session_id = args.session_id or session_id_from_github_event()
        if not args.session_id:
            print(f"{args.command}: using derived session id {session_id!r}", file=sys.stderr)

        if args.command == "invoke":
            from strandly_harness.ops.runtime_client import launch_run

            # Fire-and-forget: no GitHub context required. Returns a taskId to poll; the result
            # is read back from AgentCore Memory under this session id by `strandly poll`.
            result = launch_run(runtime_arn, region, session_id, args.prompt)
        else:
            from strandly_harness.ops.runtime_client import poll_run

            result = poll_run(runtime_arn, region, session_id, args.task_id)
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") != "error" else 1

    from strandly_harness.core.config import Config

    config = Config.load()

    if args.command == "port":
        from strandly_harness.serve.cli.repl import run_oneshot

        task = f"Translate {args.issue}"
        if args.pr:
            task = f"{task}\n\nIterate on the existing work in {args.pr}, addressing its review feedback."
        prompt = f'skill(action="activate", name="port")\n\n{task}'
        run_oneshot(config, prompt, session_id=f"strandly-port-{args.issue}", hitl=args.hitl)
        return 0

    if args.command == "brief":
        from datetime import datetime, timezone

        from strandly_harness.serve.cli.repl import run_oneshot

        task = (
            f"Produce the team morning brief covering the last {args.since}. Write it as Markdown "
            f"to {args.out} (if that path is a directory, name the file morning-brief-<today's "
            f"date>.md inside it, creating the directory if needed). Print a short TL;DR to the "
            f"terminal."
        )
        prompt = f'skill(action="activate", name="brief")\n\n{task}'
        session_id = args.session_id or f"strandly-brief-{datetime.now(timezone.utc):%Y%m%d}"
        run_oneshot(config, prompt, session_id=session_id)
        return 0

    if args.command == "run":
        from strandly_harness.serve.cli.repl import run_oneshot

        run_oneshot(config, args.prompt, session_id=args.session_id, hitl=args.hitl)
    elif args.command == "chat":
        if args.agentcore:
            from strandly_harness.serve.cli.repl import chat_agentcore
            from strandly_harness.serve.deploy import resolve_region, resolve_runtime_arn

            runtime_arn = resolve_runtime_arn(None)
            region = resolve_region(None)
            if not runtime_arn or not region or not config.memory_id:
                print(
                    "chat --agentcore needs a deployed runtime: a resolvable runtime ARN "
                    "(deploy first / --runtime-arn / STRANDLY_RUNTIME_ARN), a region, and "
                    "AGENTCORE_MEMORY_ID. Falling back is not automatic — use plain `chat` "
                    "for a local agent.",
                    file=sys.stderr,
                )
                return 1
            chat_agentcore(runtime_arn, region, args.session_id)
        else:
            from strandly_harness.serve.cli.repl import chat

            chat(config, session_id=args.session_id, hitl=args.hitl)
    elif args.command == "serve" and args.mode == "agentcore":
        from strandly_harness.serve.agentcore_app import serve_agentcore

        serve_agentcore(config)
    elif args.command == "serve" and args.mode == "mcp":
        from strandly_harness.serve.mcp_server import serve_mcp

        serve_mcp(config)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
