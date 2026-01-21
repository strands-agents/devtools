# Authorization Check Action

A reusable GitHub Action that validates user permissions and determines the appropriate approval environment for workflow execution.

## Features

- Validates user permissions via GitHub's collaborator API
- Configurable role-based authorization via `allowed-roles` input
- Supports auto-approval for users with matching roles
- Handles workflow_dispatch events with automatic approval
- Safe defaults: requires manual approval on errors or insufficient permissions
- Clear logging of authorization decisions

## Usage

```yaml
jobs:
  authorization-check:
    permissions: read-all
    runs-on: ubuntu-latest
    outputs:
      approval-env: ${{ steps.auth.outputs.approval-env }}
    steps:
      - name: Check Authorization
        id: auth
        uses: strands-agents/devtools/authorization-check@main
        with:
          skip-check: ${{ github.event_name == 'workflow_dispatch' }}
          username: ${{ github.event.comment.user.login }}
          allowed-roles: 'triage,write,admin'
  
  protected-job:
    needs: [authorization-check]
    environment: ${{ needs.authorization-check.outputs.approval-env }}
    runs-on: ubuntu-latest
    steps:
      - run: echo "This job requires approval based on user permissions"
```

## Inputs

| Input | Description | Required | Default |
|-------|-------------|----------|---------|
| `skip-check` | Skip collaborator check (for workflow_dispatch events) | No | `false` |
| `username` | Username to check | Yes | - |
| `allowed-roles` | Comma-separated list of allowed roles (e.g., "triage,write,admin") | Yes | - |

## Outputs

| Output | Description | Values |
|--------|-------------|--------|
| `approval-env` | Approval environment name | `auto-approve` or `manual-approval` |

## Authorization Logic

### Collaborator Check Mode (`skip-check: false`)

1. Calls GitHub API: `repos.getCollaboratorPermissionLevel`
2. Checks user's `role_name` against the roles specified in `allowed-roles` input
3. Returns:
   - `auto-approve`: User has a role matching one in `allowed-roles`
   - `manual-approval`: User lacks matching role or API error occurs

### Skip Check Mode (`skip-check: true`)

- Always returns `auto-approve`
- Used for workflow_dispatch events or trusted contexts
