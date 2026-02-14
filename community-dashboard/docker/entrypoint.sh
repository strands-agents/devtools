#!/bin/sh
set -e

DB_PATH="/var/lib/grafana/data/metrics.db"
CONFIG_DIR="/etc/strands"

# ── Initial backfill ────────────────────────────────────────────────────────
# If the database doesn't exist or is empty on the EFS volume, seed it from
# the pre-built copy baked into the image. The daily cron handles incremental
# updates from there — no hours-long backfill needed on first boot.
SEED_PATH="/seed/metrics.db"

if [ ! -f "$DB_PATH" ] || [ ! -s "$DB_PATH" ]; then
    if [ -f "$SEED_PATH" ]; then
        echo "[entrypoint] Seeding metrics.db from bundled snapshot..."
        cp "$SEED_PATH" "$DB_PATH"
        echo "[entrypoint] Seed copy complete ($(du -h "$DB_PATH" | cut -f1))."
        # Run a quick incremental sync to pick up anything newer than the snapshot
        if [ -n "$GITHUB_TOKEN" ]; then
            echo "[entrypoint] Running incremental sync to catch up..."
            strands-metrics --db-path "$DB_PATH" sync || \
                echo "[entrypoint] WARNING: Incremental sync failed (will retry on next cron run)."
        fi
    else
        echo "[entrypoint] No seed DB found — running full sync..."
        if [ -z "$GITHUB_TOKEN" ]; then
            echo "[entrypoint] WARNING: GITHUB_TOKEN is not set. Skipping sync."
        else
            strands-metrics --db-path "$DB_PATH" sync || \
                echo "[entrypoint] WARNING: Initial sync failed (will retry on next cron run)."
        fi
    fi
else
    echo "[entrypoint] metrics.db already exists with data — skipping seed."
fi

# ── Load configuration ─────────────────────────────────────────────────────
# Load goals and team config into the database on every startup
echo "[entrypoint] Loading goals configuration..."
strands-metrics --db-path "$DB_PATH" load-goals "$CONFIG_DIR/goals.yaml" || \
    echo "[entrypoint] WARNING: Failed to load goals."

echo "[entrypoint] Loading team configuration..."
strands-metrics --db-path "$DB_PATH" load-team "$CONFIG_DIR/team.yaml" || \
    echo "[entrypoint] WARNING: Failed to load team."

# ── Sync package downloads ─────────────────────────────────────────────────
echo "[entrypoint] Syncing package downloads..."
strands-metrics --db-path "$DB_PATH" sync-downloads --config-path "$CONFIG_DIR/packages.yaml" || \
    echo "[entrypoint] WARNING: Failed to sync downloads."

# ── Cron schedule ───────────────────────────────────────────────────────────
# Sync daily at 06:00 UTC. Output is forwarded to container stdout/stderr
# via /proc/1/fd/1 so it shows up in docker logs / CloudWatch.
CRONTAB="/tmp/crontab"
cat > "$CRONTAB" <<'CRONEOF'
0 6 * * * strands-metrics --db-path /var/lib/grafana/data/metrics.db sync >> /proc/1/fd/1 2>&1 && strands-metrics --db-path /var/lib/grafana/data/metrics.db sweep >> /proc/1/fd/1 2>&1 && strands-metrics --db-path /var/lib/grafana/data/metrics.db sync-downloads --config-path /etc/strands/packages.yaml >> /proc/1/fd/1 2>&1 && strands-metrics --db-path /var/lib/grafana/data/metrics.db load-goals /etc/strands/goals.yaml >> /proc/1/fd/1 2>&1 && strands-metrics --db-path /var/lib/grafana/data/metrics.db load-team /etc/strands/team.yaml >> /proc/1/fd/1 2>&1
CRONEOF

echo "[entrypoint] Starting supercronic (daily sync at 06:00 UTC)..."
supercronic "$CRONTAB" &

# ── Start Grafana ───────────────────────────────────────────────────────────
echo "[entrypoint] Launching Grafana..."
exec /run.sh
