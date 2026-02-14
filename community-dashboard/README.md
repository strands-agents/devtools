# community-dashboard

GitHub metrics collection and Grafana dashboards for the [strands-agents](https://github.com/strands-agents) organization.

A unified Docker container syncs GitHub data (issues, PRs, stars, commits, CI runs, reviews, comments) into a local SQLite database on a daily cron schedule, and serves pre-built Grafana dashboards for org-wide health and triage visibility.


## Directory Structure

```
community-dashboard/
├── README.md                              ← you are here
├── docker/
│   ├── Dockerfile                         ← unified Grafana + metrics-sync image
│   ├── entrypoint.sh                      ← initial backfill, cron, then Grafana
│   └── docker-compose.local.yaml          ← local dev compose
├── provisioning/                           ← Grafana auto-provisioning
│   ├── datasources/
│   │   └── automatic.yaml                 ← SQLite datasource
│   └── dashboards/
│       ├── dashboards.yaml                ← dashboard provider config
│       ├── health.json                    ← org health dashboard
│       └── triage.json                    ← triage dashboard
├── strands-metrics/                        ← Rust CLI (syncs GitHub → SQLite)
│   ├── Cargo.toml
│   └── src/
│       ├── main.rs
│       ├── client.rs
│       ├── db.rs
│       └── aggregates.rs
└── cdk/                                    ← AWS CDK deployment
    ├── bin/app.ts
    ├── lib/community-dashboard-stack.ts
    ├── package.json
    ├── tsconfig.json
    ├── cdk.json
    └── .env.example
```

## Prerequisites

| Tool | Purpose |
|------|---------|
| **Docker** + **Docker Compose** | Build & run the unified container |
| **GitHub PAT** | Token with read access to the `strands-agents` org (public repos) |
| **Node.js** ≥ 18 | CDK CLI (AWS deployment only) |
| **AWS CDK CLI** | `npm install -g aws-cdk` (AWS deployment only) |
| **Rust toolchain** | Only needed if building strands-metrics locally outside Docker |

## Local Development

Build and run the unified container locally:

```bash
cd community-dashboard
GITHUB_TOKEN=ghp_your_token docker compose -f docker/docker-compose.local.yaml up --build
```

On first start the container will:
1. Run a full GitHub sync (this takes a few minutes)
2. Start a daily cron job (06:00 UTC) for incremental syncs
3. Launch Grafana

Open [http://localhost:3000](http://localhost:3000) — no login required (anonymous read-only).

The SQLite database is persisted in `docker/data/` on the host so subsequent restarts skip the initial backfill.

### Running strands-metrics standalone

If you prefer to run the Rust CLI directly (without Docker):

```bash
cd strands-metrics
GITHUB_TOKEN=ghp_xxx cargo run --release -- sync       # full/incremental sync
GITHUB_TOKEN=ghp_xxx cargo run --release -- sweep      # reconcile stale open items
cargo run --release -- query "SELECT date, stars FROM daily_metrics WHERE repo='sdk-python' ORDER BY date DESC LIMIT 10"
```

By default the CLI writes to `../metrics.db` (the `community-dashboard/` root).

## AWS Deployment

The CDK stack deploys everything to AWS as a single Fargate service with EFS-backed persistent storage:

```
CloudFront (HTTPS) → ALB (HTTP:80) → ECS Fargate → unified Docker image → EFS (metrics.db)
```

### 1. Create the GitHub token secret

```bash
aws secretsmanager create-secret \
  --name strands-grafana/github-token \
  --secret-string "ghp_your_token" \
  --region us-west-2
```

### 2. Configure and deploy

```bash
cd cdk
cp .env.example .env
# Edit .env — set GITHUB_SECRET_ARN to the ARN from step 1

npm install
npx cdk deploy
```

The stack creates:
- **VPC** (2 AZs, 1 NAT gateway)
- **EFS** file system with access point at `/grafana-data` (RETAIN policy)
- **ECS Fargate** service (0.5 vCPU, 1 GB RAM)
- **ALB** on port 80 with health check at `/api/health`
- **CloudFront** distribution for HTTPS access

The Grafana URL (HTTPS) is printed in the stack outputs.

### Tear down

```bash
cd cdk
npx cdk destroy
```

> **Note:** The EFS file system has a RETAIN removal policy — delete it manually if you want to remove the data.

## Dashboards

- **Health** — org-wide metrics: stars, open issues & PRs, merge times (internal vs external), CI health, code churn, time-to-first-response
- **Triage** — focused view for issue/PR triage workflows
