# strands-grafana

GitHub metrics collection and Grafana dashboards for the [strands-agents](https://github.com/strands-agents) organization.

This tool syncs GitHub data (issues, PRs, stars, commits, CI runs, etc.) into a local SQLite database, then visualizes it through pre-built Grafana dashboards. It provides org-wide health metrics including time-to-first-response, merge times, open item counts, star growth, and more.

> Originally created by [@chaynabors](https://github.com/chaynabors) — migrated from [chaynabors/strands](https://github.com/chaynabors/strands) (commit `0fbe13c`).

## Directory Structure

```
strands-grafana/
├── README.md                  ← you are here
├── docker-compose.yaml        ← Grafana container config
├── provisioning/              ← Grafana auto-provisioning
│   ├── datasources/
│   │   └── automatic.yaml     ← SQLite datasource config
│   └── dashboards/
│       ├── dashboards.yaml    ← dashboard provider config
│       ├── health.json        ← org health dashboard
│       └── triage.json        ← triage dashboard
└── strands-metrics/           ← Rust CLI for syncing GitHub data
    ├── Cargo.toml
    └── src/
        ├── main.rs
        ├── client.rs
        ├── db.rs
        └── aggregates.rs
```

## Prerequisites

- **Rust toolchain** — install via [rustup](https://rustup.rs/)
- **Docker** and **Docker Compose** — for running Grafana
- **GitHub personal access token** — with read access to the `strands-agents` org (public repos)

## Usage

### 1. Sync GitHub metrics

From the `strands-grafana` directory:

```bash
cd strands-metrics
GITHUB_TOKEN=ghp_your_token_here cargo run --release -- sync
```

This will:
- Fetch all public repos in the `strands-agents` org
- Sync issues, PRs, reviews, comments, stars, commits, and CI workflow runs
- Compute daily aggregate metrics
- Write everything to `../metrics.db` (i.e. `strands-grafana/metrics.db`)

Subsequent runs are incremental — only new/updated data is fetched.

#### Other commands

```bash
# Sweep: reconcile locally-open items against GitHub (mark deleted/closed items)
GITHUB_TOKEN=ghp_xxx cargo run --release -- sweep

# Query: run arbitrary SQL against the database
cargo run --release -- query "SELECT date, stars FROM daily_metrics WHERE repo = 'sdk-python' ORDER BY date DESC LIMIT 10"
```

### 2. Launch Grafana

From the `strands-grafana` directory:

```bash
docker-compose up
```

Then open [http://localhost:3000](http://localhost:3000) in your browser.

Grafana is configured for anonymous read-only access — no login required. The SQLite database is mounted read-only into the container.

### 3. Dashboards

- **Health** — org-wide metrics: stars, open issues/PRs, merge times, CI health, code churn, response times
- **Triage** — focused view for issue/PR triage workflows

## Notes

- `metrics.db` is gitignored — you must run the sync yourself to populate it
- The sync respects GitHub API rate limits and will pause automatically when limits are low
- The default db path when running from `strands-metrics/` is `../metrics.db`, placing it in the `strands-grafana/` root where `docker-compose.yaml` expects it
