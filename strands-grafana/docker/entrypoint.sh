#!/bin/sh
set -e

DB_PATH="/var/lib/grafana/data/metrics.db"

# ── Initial backfill ────────────────────────────────────────────────────────
# If the database doesn't exist yet, run a full sync before starting Grafana
# so the dashboards have data from the first boot.
if [ ! -f "$DB_PATH" ]; then
    echo "[entrypoint] metrics.db not found — running initial sync..."
    if [ -z "$GITHUB_TOKEN" ]; then
        echo "[entrypoint] WARNING: GITHUB_TOKEN is not set. Skipping initial sync."
    else
        strands-metrics --db-path "$DB_PATH" sync || \
            echo "[entrypoint] WARNING: Initial sync failed (will retry on next cron run)."
    fi
else
    echo "[entrypoint] metrics.db already exists — skipping initial sync."
fi

# ── Cron schedule ───────────────────────────────────────────────────────────
# Sync daily at 06:00 UTC. Output is forwarded to container stdout/stderr
# via /proc/1/fd/1 so it shows up in docker logs / CloudWatch.
CRONTAB="/tmp/crontab"
cat > "$CRONTAB" <<'EOF'
0 6 * * * strands-metrics --db-path /var/lib/grafana/data/metrics.db sync >> /proc/1/fd/1 2>&1
EOF

echo "[entrypoint] Starting supercronic (daily sync at 06:00 UTC)..."
supercronic "$CRONTAB" &

# ── Start Grafana ───────────────────────────────────────────────────────────
echo "[entrypoint] Launching Grafana..."
exec /run.sh
