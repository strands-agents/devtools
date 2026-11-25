# strandly-mcp

Use a **deployed** Strands Factory agent from any MCP client (Claude Code, Kiro, Cursor, Q CLI, another
Strands agent) — one browser login, no AWS credentials, no agent stack.

The package is not published to PyPI; build the wheel from the repository
(`uv build --package strandly-mcp --wheel`). Install it into an isolated tool environment, then
configure and log in to your own deployment:

```bash
uv tool install ./strandly_mcp-*.whl
strandly-mcp login
```

```jsonc
// mcp.json — after `uv tool install` puts `strandly-mcp` on PATH
{ "mcpServers": { "strandly": {
  "command": "strandly-mcp",
  "args": ["serve"]
} } }
```

Two tools. `ask(prompt, session?, wait?)` talks to the agent; `session(id?)` lists its recent
conversation threads or reads one. Pass a GitHub issue / PR / discussion **URL** as `session` to
join the agent's thread for that item — the same thread the mention poller, GitHub Actions and the
dashboard use. `wait=false` starts a long run and returns at once; `session(id)` reads its result.

This package is deliberately thin (`mcp` + stdlib; `boto3` only via
`strandly-mcp[aws]` for `serve --agentcore`). It never imports the agent runtime,
`strands`, or `strands_harness`. Its wheel is tested in a clean environment.
