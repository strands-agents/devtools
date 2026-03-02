# Project Overview

Community Dashboard is a GitHub metrics collection and visualization
platform for the `strands-agents` open-source organization. A Rust CLI
(`strands-metrics`) pulls data from GitHub, PyPI, and npm into a SQLite
database; Grafana renders seven pre-built dashboards across three
folders (General, SDKs, Operations). The stack runs locally via Docker
Compose or deploys to AWS with CDK (Fargate + EFS + CloudFront).

## Repository Structure

- `strands-metrics/` — Rust CLI that syncs GitHub/PyPI/npm data into
  SQLite and computes daily aggregate metrics.
- `docker/` — Split Dockerfiles (`Dockerfile.grafana` for serving,
  `Dockerfile.metrics` for Rust build + sync), Compose file,
  entrypoint, and daily sync script.
- `provisioning/` — Grafana provisioning configs: datasource
  (SQLite) and seven dashboard JSON files in three folders.
- `cdk/` — AWS CDK stack (TypeScript): VPC, ECS Fargate, EFS,
  API Gateway, CloudFront.
- `goals.yaml` — Goal thresholds shown as lines on Health dashboard.
- `team.yaml` — Team members tracked in Team Performance dashboard.
- `packages.yaml` — Repo-to-registry mappings for download tracking.

## Build & Development Commands

### Local development (Docker)

```bash
# Build and run with auto-sync (opens at http://localhost:3000)
GITHUB_TOKEN=ghp_xxx docker compose \
  -f docker/docker-compose.local.yaml up --build
```

### Standalone Rust CLI

```bash
cd strands-metrics

# Build
cargo build --release

# Incremental GitHub sync (PRs, issues, stars, commits, CI, reviews)
GITHUB_TOKEN=ghp_xxx cargo run --release -- sync

# Garbage-collect stale open items
GITHUB_TOKEN=ghp_xxx cargo run --release -- sweep

# Sync recent PyPI/npm downloads (default 30 days)
cargo run --release -- sync-downloads

# Backfill historical downloads (PyPI ~180d, npm ~365d)
cargo run --release -- backfill-downloads

# Backfill triage timestamps from GitHub timeline API
GITHUB_TOKEN=ghp_xxx cargo run --release -- backfill-triage

# Load goal thresholds / team members
cargo run --release -- load-goals
cargo run --release -- load-team

# Ad-hoc SQL query
cargo run --release -- query "SELECT repo, COUNT(*) FROM pull_requests GROUP BY repo"
```

### CDK deployment

```bash
cd cdk
cp .env.example .env          # set GITHUB_SECRET_ARN
npm install
npx cdk deploy
```

### Lint / format (Rust)

```bash
cd strands-metrics
cargo fmt --check
cargo clippy -- -D warnings
```

### CDK type-check

```bash
cd cdk
npx tsc --noEmit
```

## Code Style & Conventions

- Rust edition 2021; format with `cargo fmt`, lint with `cargo clippy`.
- CDK is TypeScript; standard `tsc` strict mode.
- Grafana dashboards are provisioned as JSON files under
  `provisioning/dashboards/`. Edit JSON directly or export from
  Grafana UI and commit.
- YAML configs (`goals.yaml`, `team.yaml`, `packages.yaml`) use
  lowercase-snake_case keys.
- Shell scripts use `#!/bin/sh`, `set -e`, and log with bracketed
  prefixes like `[entrypoint]` or `[sync-all]`.

> TODO: No commit-message template is defined. Consider adopting
> Conventional Commits (`feat:`, `fix:`, `chore:`).

## Architecture Notes

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  GitHub API  │  │  PyPI Stats  │  │ npm Registry │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       ▼                 ▼                 ▼
   ┌─────────────────────────────────────────┐
   │       strands-metrics CLI (Rust)        │
   │  client.rs │ downloads.rs │ goals.rs    │
   │  db.rs     │ aggregates.rs              │
   └──────────────────┬──────────────────────┘
                      │
                      ▼
              ┌───────────────┐
              │  metrics.db   │
              │   (SQLite)    │
              └───────┬───────┘
                      │
                      ▼
              ┌───────────────┐
              │    Grafana    │
              │ (SQLite plugin)│
              └───────┬───────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
   General/       SDKs/       Operations/
   executive    python-sdk      team
   health       typescript-sdk  triage
                evals
```

### Rust modules

| Module | Responsibility |
|---|---|
| `main.rs` | CLI entry point (clap), dispatches subcommands, TTY-aware logging |
| `client.rs` | GitHub API client via octocrab, GraphQL with retry/backoff, dual progress (spinner + tracing) |
| `db.rs` | SQLite schema init and migrations |
| `downloads.rs` | PyPI / npm download stat fetching |
| `goals.rs` | Goal thresholds and team member management |
| `aggregates.rs` | Daily metric computation from raw data |

### AWS deployment path

```
CloudFront (HTTPS, WAF rate-limit: 300 req/5min per IP)
  → API Gateway HTTP API (VPC Link)
    → ECS Fargate — Grafana Service (always-on, 0.5 vCPU / 1 GB)
        → EFS (metrics.db, RETAIN policy)

EventBridge (daily 06:00 UTC)
  → ECS Fargate — Metrics Task (on-demand, 0.5 vCPU / 1 GB)
      → EFS (writes metrics.db)
      → Secrets Manager (GITHUB_TOKEN)
```

The architecture splits serving from syncing: Grafana runs as an
always-on service, while the metrics sync runs as a scheduled
Fargate task triggered by EventBridge. WAF rate-limiting on
CloudFront prevents runaway costs from external traffic spikes.

## Testing Strategy

> TODO: No unit or integration tests exist for the Rust CLI.
> Consider adding tests for `aggregates.rs` computations and
> `goals.rs` YAML parsing.

The GitHub Actions workflow (`.github/workflows/community-dashboard.yaml`)
runs daily at 06:00 UTC as a de-facto integration test:

1. `cargo run --release -- sync`
2. `cargo run --release -- sweep`
3. `cargo run --release -- sync-downloads`
4. `cargo run --release -- load-goals`
5. `cargo run --release -- load-team`
6. Commits updated `metrics.db` back to the repo.

Required secret: `METRICS_PAT` (GitHub PAT with org read access
and `read:project` scope for project items sync).

## Security & Compliance

- `GITHUB_TOKEN` is the only secret; never commit it.
  - Locally: pass via env var or `.env` file (gitignored).
  - AWS: stored in Secrets Manager, injected into ECS via CDK.
  - CI: stored as `METRICS_PAT` GitHub Actions secret.
- Grafana runs in anonymous viewer-only mode
  (`GF_AUTH_ANONYMOUS_ENABLED=true`, `GF_AUTH_BASIC_ENABLED=false`).
  No login form, no sign-up.
- EFS volume is encrypted at rest with `RETAIN` removal policy.
- Docker images are split: `Dockerfile.grafana` is based on
  `grafana/grafana:latest` (Alpine); `Dockerfile.metrics` is a
  multi-stage Rust build → minimal Alpine runner.
- SQLite datasource is mounted read-only in Grafana
  (`mode: ro` in `automatic.yaml`).
- WAF WebACL on CloudFront rate-limits to 300 requests per
  5-minute window per IP, preventing cost spikes from abuse.

> TODO: No dependency scanning (e.g., `cargo audit`, Dependabot)
> is configured.

## Agent Guardrails

- Do NOT push to `strands-agents/devtools` directly; use forks.
- Never commit `metrics.db` manually; the CI workflow handles it.
- Never commit `.env` files or tokens.
- Dashboard JSON files under `provisioning/dashboards/` are
  auto-loaded by Grafana. Validate JSON before committing.
- Do not modify `provisioning/datasources/automatic.yaml` unless
  the database path or plugin changes.
- The CDK stack uses `RemovalPolicy.RETAIN` on EFS. Do not change
  this to `DESTROY` without explicit approval.
- Avoid modifying `docker/entrypoint.sh` seed logic without
  testing locally first — a broken entrypoint blocks the entire
  dashboard.

## Extensibility Hooks

- Add new dashboards by placing JSON files in the appropriate
  `provisioning/dashboards/{general,sdks,operations}/` folder.
  Grafana picks them up automatically (60s poll).
- Add new CLI subcommands in `strands-metrics/src/main.rs`
  (clap derive).
- Track new packages by adding entries to `packages.yaml`.
- Add new goal metrics by adding entries to `goals.yaml` and
  running `load-goals`.
- Add team members via `team.yaml` and running `load-team`.
- Environment variables:
  - `GITHUB_TOKEN` — required for sync/sweep commands.
  - `GF_*` — standard Grafana env var overrides.

## Further Reading

- [README.md](README.md) — Full quick-start, CLI reference,
  configuration docs, and deployment guide.
- [goals.yaml](goals.yaml) — Goal threshold configuration
  reference with inline documentation.
- [cdk/](cdk/) — CDK stack source and `.env.example`.
- [docker/](docker/) — Dockerfile, Compose, entrypoint, and
  sync scripts.
- [provisioning/](provisioning/) — Grafana datasource and
  dashboard provisioning configs.
