# Community Dashboard

GitHub metrics dashboards for the `strands-agents` organization. Collects data from GitHub, PyPI, and npm into a SQLite database, then visualizes it through pre-built Grafana dashboards.

Deployable locally via Docker Compose or to AWS via CDK (Fargate + EFS + CloudFront).

## Directory Structure

```
community-dashboard/
├── Cargo.toml                    # Rust CLI project
├── src/                          # strands-metrics CLI source
│   ├── main.rs                   # CLI entry point (sync, sweep, query, etc.)
│   ├── client.rs                 # GitHub API client (octocrab)
│   ├── db.rs                     # SQLite schema & initialization
│   ├── downloads.rs              # PyPI/npm download tracking
│   ├── goals.rs                  # Goal thresholds & team management
│   └── aggregates.rs             # Daily metric computation
├── goals.yaml                    # Configurable goal thresholds
├── team.yaml                     # Team members for performance tracking
├── packages.yaml                 # Package-to-registry mappings
├── docker-compose.yaml           # Quick local Grafana (read-only)
├── docker/
│   ├── Dockerfile                # Unified Grafana + metrics-sync image
│   ├── docker-compose.local.yaml # Local dev with auto-sync
│   └── entrypoint.sh             # Container startup script
├── provisioning/
│   ├── datasources/
│   │   └── automatic.yaml        # SQLite datasource config
│   └── dashboards/
│       ├── dashboards.yaml       # Dashboard folder provider config
│       ├── general/              # Top-level dashboards
│       │   ├── executive.json    #   Executive Summary
│       │   └── health.json       #   Org Health
│       ├── sdks/                 # SDK-specific dashboards
│       │   ├── evals.json        #   Evaluations
│       │   ├── python-sdk.json   #   Python SDK
│       │   └── typescript-sdk.json # TypeScript SDK
│       └── operations/           # Operations dashboards
│           ├── team.json         #   Team Performance
│           └── triage.json       #   Triage
└── cdk/                          # AWS CDK deployment stack
    ├── bin/app.ts
    ├── lib/community-dashboard-stack.ts
    ├── package.json
    └── cdk.json
```

## Quick Start (Local)

### Option A: Docker Compose with existing database

If you already have a `metrics.db`, place it in this directory and run:

```bash
docker compose up
```

Open [http://localhost:3000](http://localhost:3000). Grafana starts in anonymous viewer mode with all dashboards pre-loaded.

### Option B: Docker with auto-sync

Build the unified image that syncs GitHub data automatically:

```bash
GITHUB_TOKEN=ghp_xxx docker compose -f docker/docker-compose.local.yaml up --build
```

This builds the Rust CLI, runs an initial sync on startup, and schedules daily updates at 06:00 UTC via supercronic.

### Option C: Standalone CLI

Build and run `strands-metrics` directly:

```bash
# Build
cargo build --release

# Sync GitHub data (PRs, issues, stars, commits, CI runs, reviews)
GITHUB_TOKEN=ghp_xxx cargo run --release -- sync

# Garbage collection (reconcile stale open items)
GITHUB_TOKEN=ghp_xxx cargo run --release -- sweep

# Sync PyPI/npm download stats
cargo run --release -- sync-downloads

# Load goal thresholds into the database
cargo run --release -- load-goals goals.yaml

# Load team members for the Team dashboard
cargo run --release -- load-team team.yaml

# Backfill historical downloads (PyPI: ~180 days, npm: ~365 days)
cargo run --release -- backfill-downloads

# Run arbitrary SQL queries
cargo run --release -- query "SELECT repo, SUM(prs_merged) FROM daily_metrics GROUP BY repo"
```

Then start Grafana to visualize:

```bash
docker compose up
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `sync` | Incremental sync of GitHub data (PRs, issues, stars, commits, CI, reviews, comments) |
| `sweep` | Garbage collection -- checks open items against GitHub and marks missing ones as deleted |
| `query <sql>` | Run raw SQL against the metrics database |
| `load-goals [path]` | Load goal thresholds from YAML into the database |
| `list-goals` | Display all configured goal thresholds |
| `load-team [path]` | Load team members from YAML (or `--members alice,bob`) |
| `sync-downloads` | Sync recent package downloads from PyPI and npm (default: 30 days) |
| `backfill-downloads` | Backfill historical download data (PyPI: ~180 days, npm: ~365 days) |

### Global flags

| Flag | Default | Description |
|------|---------|-------------|
| `--db-path` / `-d` | `metrics.db` | Path to the SQLite database file |

## Dashboards

### General

- **Executive Summary** -- High-level org overview: total stars, open PRs/issues, stale PR count, contributor trends
- **Health** -- Org health metrics with goal lines: merge time, cycle time, CI failure rate, community PR %, contributor retention, response times

### SDKs

- **Python SDK** -- Python SDK-specific metrics: PRs, issues, stars, downloads from PyPI
- **TypeScript SDK** -- TypeScript SDK metrics with npm download tracking
- **Evaluations** -- Evals framework metrics

### Operations

- **Team Performance** -- Per-member activity tracking: PRs opened/merged, reviews given, issues closed
- **Triage** -- Open issues and PRs requiring attention, sorted by staleness

## Configuration

### goals.yaml

Defines target thresholds that appear as goal lines on Health dashboard panels:

```yaml
goals:
  avg_merge_time_hours:
    value: 24
    label: "Goal (24h)"
    direction: lower_is_better    # green below, red above
    # warning_ratio: 0.75         # optional, default varies by direction
```

Each goal requires:
- `value` -- The target threshold
- `label` -- Display label for the goal line
- `direction` -- `lower_is_better` or `higher_is_better`
- `warning_ratio` -- (optional) Multiplier for warning threshold (default: 0.75 for lower, 0.70 for higher)

### team.yaml

Lists team members tracked in the Team Performance dashboard:

```yaml
members:
  - username: alice
  - username: bob
```

### packages.yaml

Maps GitHub repos to their published packages for download tracking:

```yaml
repo_mappings:
  sdk-python:
    - package: strands-agents
      registry: pypi
  sdk-typescript:
    - package: "@strands-agents/sdk"
      registry: npm
```

## AWS Deployment (CDK)

### Prerequisites

1. AWS CLI configured with appropriate credentials
2. Node.js 18+
3. A GitHub PAT stored in AWS Secrets Manager:
   ```bash
   aws secretsmanager create-secret \
     --name strands-grafana/github-token \
     --secret-string "ghp_xxx" \
     --region us-west-2
   ```

### Deploy

```bash
cd cdk
cp .env.example .env
# Edit .env with your GITHUB_SECRET_ARN

npm install
npx cdk deploy
```

### Architecture

```
CloudFront (HTTPS) -> ALB (HTTP:80) -> ECS Fargate -> Grafana + strands-metrics -> EFS (metrics.db)
```

- **CloudFront** for HTTPS without needing ACM + custom domain
- **EFS with RETAIN policy** so metrics survive redeployments
- **Fargate** (0.5 vCPU / 1 GB) with daily cron via supercronic
- **Anonymous viewer-only** Grafana with `ALLOW_EMBEDDING=true` for iframes

## GitHub Actions Workflow

The included workflow (`.github/workflows/community-dashboard.yaml`) runs daily at 06:00 UTC:

1. Syncs GitHub data (PRs, issues, stars, commits, CI, reviews)
2. Runs garbage collection (sweep)
3. Syncs PyPI/npm download stats
4. Loads goals and team configuration
5. Commits the updated `metrics.db` back to the repository

Required secret: `METRICS_PAT` -- a GitHub PAT with read access to the `strands-agents` org.

## Data Flow

```
GitHub API (octocrab)     PyPI Stats API     npm Registry API
        |                      |                    |
        v                      v                    v
    strands-metrics CLI (Rust)
        |
        v
    metrics.db (SQLite)
        |
        v
    Grafana (SQLite datasource plugin)
        |
        v
    7 pre-built dashboards in 3 folders
```
