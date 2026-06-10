# Issue Labeler

A reusable GitHub Action that classifies issues using an LLM (via the [Strands Agents SDK](https://github.com/strands-agents/sdk-python)) and applies labels from a hardcoded allowlist.

## Security Model

The LLM has no tools, no shell access, and no GitHub API access. It returns a structured object whose label field is an enum built from the label names in your config file, so out-of-allowlist values are rejected by the schema rather than filtered after the fact. The worst a prompt injection can achieve is mislabeling — never arbitrary label creation or other side effects.

| Layer | Protection |
|-------|-----------|
| Input | Sanitized (control chars stripped) and truncated before reaching the LLM |
| Output | Structured output constrained to an allowlist enum, capped at `max_labels` |
| IAM | Scoped to `bedrock:InvokeModel` / `bedrock:InvokeModelWithResponseStream` |
| Permissions | Workflow only needs `issues: write` |

## Usage

### Single labeler (file config)

```yaml
# .github/workflows/issue-labeler.yml
on:
  issues:
    types: [opened, edited]

permissions:
  issues: write
  id-token: write
  contents: read

jobs:
  label:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          sparse-checkout: .github/labelers
          sparse-checkout-cone-mode: false
      - uses: strands-agents/devtools/issue-labeler@main
        with:
          aws_role_arn: ${{ secrets.AWS_ROLE_ARN }}
          config_path: '.github/labelers/area.yml'
```

### Single labeler (inline config)

```yaml
- uses: strands-agents/devtools/issue-labeler@main
  with:
    aws_role_arn: ${{ secrets.AWS_ROLE_ARN }}
    max_labels: '1'
    config: |
      labels:
        bug: "Something is broken"
        enhancement: "New feature or improvement"
        question: "User needs help"
```

### Multiple independent labelers

Run separate jobs for each concern. They execute in parallel and don't conflict:

```yaml
jobs:
  label-area:
    steps:
      - uses: strands-agents/devtools/issue-labeler@main
        with:
          aws_role_arn: ${{ secrets.AWS_ROLE_ARN }}
          config_path: '.github/labelers/area.yml'

  label-type:
    steps:
      - uses: strands-agents/devtools/issue-labeler@main
        with:
          aws_role_arn: ${{ secrets.AWS_ROLE_ARN }}
          config_path: '.github/labelers/type.yml'
          max_labels: '1'

  label-language:
    steps:
      - uses: strands-agents/devtools/issue-labeler@main
        with:
          aws_role_arn: ${{ secrets.AWS_ROLE_ARN }}
          config_path: '.github/labelers/language.yml'
          max_labels: '1'
```

## Config File Format

```yaml
# Optional: extra context appended to the system prompt
instructions: |
  CI dependency bumps should be labeled "chore".

# Required: label allowlist. Keys are exact label names.
# Values can be a string (description) or an object with a "description" key.
labels:
  area-mcp:
    description: "Model Context Protocol, MCP servers/clients/transport"
  area-provider:
    description: "Model providers (Bedrock, OpenAI, Anthropic, Ollama)"
  # Shorthand form:
  area-tool: "Tool behavior, execution, @tool decorator"
```

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `aws_role_arn` | Yes | - | AWS IAM role for Bedrock |
| `config` | No* | - | Inline YAML config |
| `config_path` | No* | - | Path to config file |
| `aws_region` | No | `us-west-2` | Bedrock region |
| `model_id` | No | `global.anthropic.claude-sonnet-4-6` | Bedrock inference profile for classification |
| `max_labels` | No | `3` | Max labels per issue |
| `max_body_length` | No | `1000` | Max chars of body sent to LLM |

*One of `config` or `config_path` is required.

## Outputs

| Output | Description |
|--------|-------------|
| `labels` | Comma-separated labels applied (empty string if none) |

## Prerequisites

1. An AWS IAM role that the GitHub OIDC provider can assume
2. The role needs `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` permissions
3. Bedrock access enabled for the configured model in `aws_region`
4. The labels referenced in your config must already exist on the repo
