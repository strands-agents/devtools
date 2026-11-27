---
title: "Quickstart"
description: "Install a deployment's client CLI, sign in to it, chat with the agent, and connect an editor over ACP or an MCP client."
---

# Quickstart

This page gets you from nothing to a first answer from a Strands Factory agent. The agent runs only as a deployment; this page covers using one someone already runs. To stand up your own, see [Deploy your own](deploy.md).

## Use a deployment

You need Python 3.10+, [pipx](https://pipx.pypa.io/), and a portal account on the deployment. Portal accounts are created by the operator; there is no self sign-up.

### Install the client CLI

Each deployment ships its own client CLI wheel, built from the deployment's profile. Ask your operator for it, then install it:

```bash
pipx install ./strandly_example_client-*.whl
```

The command name (`strandly-example` here) is set by the operator. The wheel contains only the client: it depends on `mcp`, `httpx`, and `agent-client-protocol`, and never installs the agent.

### Sign in

```bash
strandly-example login
```

A browser window opens on the deployment's Cognito sign-in page. After you sign in, the CLI stores the tokens in `~/.strandly/clients/strandly-example/auth.json` with mode `0600`. Each generated CLI keeps its own file, so CLIs for different deployments never share a login.

Then `login` checks who you are with GitHub: it reads a token from `gh auth token` or `GITHUB_TOKEN`, calls GitHub `GET /user` from your machine, and saves your login in `identity.json` next to `auth.json`. The token never reaches the deployment. Without a token, set the login by hand with `strandly-example identity --github <login>`. See [Tell the agent who you are](using-strands-factory.md#tell-the-agent-who-you-are).

Check which deployment you are signed in to:

```bash
strandly-example whoami
```

`whoami` prints the deployment, the access-token expiry, who you are (`you: @login (verified via gh <date>)` or `(set by hand)`), and the person your Cognito user is bound to. It never prints tokens.

### Chat

Open an interactive session:

```bash
strandly-example chat
```

Or send one message and exit:

```bash
strandly-example chat -p "Summarize the open issues labeled bug in strands-agents/sdk-python"

# Typical output:
# There are 12 open issues labeled bug. The most active are ...
```

Every chat is a session. List recent sessions, read one back, or rejoin it:

```bash
strandly-example sessions
strandly-example sessions <session-id> --tools
strandly-example chat -s <session-id>
```

The same sessions appear in the web portal, and portal sessions appear here.

### Connect an editor or MCP client

The CLI is also a stdio server for two protocols. Both reuse the login from the previous step.

| You use | Configure your client to launch |
|---|---|
| An ACP client, such as Zed | `strandly-example acp` |
| An MCP client, such as Claude Code or Kiro | `strandly-example mcp` |

For example, an MCP client entry looks like this:

```json
{
  "mcpServers": {
    "strandly": { "command": "strandly-example", "args": ["mcp"] }
  }
}
```

The MCP server exposes two tools: `ask` (send a prompt, optionally into an existing session) and `session` (read a session back). ACP is remote task delegation: the agent works in its deployed workspace, not in your local files, and there is no per-tool approval prompt. See [Using Strands Factory](using-strands-factory.md#acp-and-mcp) before you point an editor at it.

## Build a client CLI for your team

If you operate a deployment, you generate the wheel your team installs. Add a `client` section to the deployment's profile:

```json
"client": {
  "command": "strandly-example",
  "portal_url": "https://portal.example.com",
  "default_org": "example-org"
}
```

Then build it from a checkout:

```bash
uv run --no-sync strands-factory cli build --profile my-deployment.json --out dist
```

The build reads only the `client` section, refuses `REQUIRED_*` placeholders, and produces the same bytes for the same profile. `portal_url` is the Dashboard stack's `PortalURL` output; `login` discovers the API and Cognito settings from the portal's `config.json`. To check a wheel end to end against a local stub API, run `python cli/scripts/smoke_generated_cli.py <path-to-installed-command>`.

## Next steps

- [Using Strands Factory](using-strands-factory.md): every surface, schedules, and who can do what on each.
- [Architecture](architecture.md): how a request travels from a surface to the sandbox.
- [Deploy your own](deploy.md): the profile, the CDK stacks, and the deploy order.
