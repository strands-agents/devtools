<div align="center">
  <a href="https://strandsagents.com">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://strandsagents.com/latest/assets/wordmark-github-dark.svg">
      <img src="https://strandsagents.com/latest/assets/wordmark-github-light.svg" alt="Strands" width="320">
    </picture>
  </a>
</div>

# Strands Factory

Strands Factory is an open-source reference architecture for a software factory built on [Strands Agents](https://strandsagents.com). It deploys an autonomous maintainer agent that reviews pull requests, triages and reproduces issues, implements fixes, and runs scheduled work across the repositories you allow. You talk to it from a web portal, a terminal, an editor (ACP), an MCP client, Slack DMs, or a GitHub `@mention`. Every surface reaches the same agent, and every shell command the agent runs executes in a separate sandbox runtime with narrower credentials than the agent itself.

Strandly, the Strands team's agent, runs on Strands Factory.

![System overview. People reach the agent from the portal, CLI, ACP, and MCP clients, Slack DMs, and GitHub mentions, and EventBridge schedules start runs on their own. Every request passes an ingress that authorizes the caller and invokes one agent runtime on an AgentCore microVM in your AWS account. The agent keeps state in S3, DynamoDB, and AgentCore Memory, sends every command to a separate sandbox runtime on its own EC2 instance per session, and posts comments and pull requests to GitHub, while the sandbox clones and pushes with git.](docs/assets/diagrams/system-overview.svg)

*Every surface goes through an ingress to one agent runtime; the sandbox runs the commands.*

Ingresses authorize the caller before anything reaches the agent. The agent decides what to do; the sandbox is where it does it.

## Quickstart

### Use a deployment

Your operator gives you a client CLI wheel built for their deployment. Install it, sign in once in the browser, and chat:

```bash
pipx install ./strandly_example_client-*.whl
strandly-example login
strandly-example chat
```

The command name comes from the deployment's profile. The same CLI also serves ACP (`strandly-example acp`) and MCP (`strandly-example mcp`) over stdio. See [Quickstart](docs/quickstart.md).

### Deploy your own

A deployment is a profile JSON file plus a set of AWS CDK stacks. Follow [Deploy your own](docs/deploy.md), which includes a complete example profile. A fresh deployment has no GitHub write access and nobody can invoke it until you list repositories and people in the profile.

## Documentation

| Page | Read it when you want to |
|---|---|
| [Quickstart](docs/quickstart.md) | Install the CLI, sign in, and get a first answer |
| [Using Strands Factory](docs/using-strands-factory.md) | Pick a surface, schedule work, and know who can do what |
| [Architecture](docs/architecture.md) | Understand the runtimes, the request flow, and the trust boundaries |
| [Deploy your own](docs/deploy.md) | Stand up Strands Factory in your AWS account |
| [Operations](docs/operations.md) | Run the sandbox, watch alarms, and fix common failures |
| [Security model](docs/security.md) | Decide what to give the agent, see what it stores, and know how to shut it off |

Every page stands alone. Point your coding agent at this folder and ask it questions.

## Development

```bash
uv sync --locked --all-extras
python build.py web          # packaged portal assets; the package tests need them
uv run --no-sync ruff check .
uv run --no-sync mypy harness/src/strandly cli/src/strandly_cli
uv run --no-sync pytest
```

Tests use a fake model and never call AWS. The CDK stacks have their own dependency set in `infra/requirements-dev.txt`, and the portal builds with `npm ci && npm run build` in `dashboard/web-next`.

## Contributing

Strands Factory is experimental. Treat the agent's comments and pull requests as suggestions; a maintainer approves everything before it merges. Bug reports and pull requests are welcome in the [devtools repository](https://github.com/strands-agents/devtools/issues).

## License

[Apache License 2.0](LICENSE).

## Security

See [SECURITY.md](SECURITY.md) to report a vulnerability and [Security model](docs/security.md) for what the agent can reach, what it stores, and what you own.
