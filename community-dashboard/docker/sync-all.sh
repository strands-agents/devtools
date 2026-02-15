#!/bin/sh
# Full metrics sync pipeline.
# Called by supercronic daily and can be run manually.
set -e

DB_PATH="/var/lib/grafana/data/metrics.db"
CONFIG_DIR="/etc/strands"

echo "[sync-all] Starting GitHub data sync..."
strands-metrics --db-path "$DB_PATH" sync

echo "[sync-all] Running garbage collection..."
strands-metrics --db-path "$DB_PATH" sweep

echo "[sync-all] Syncing package downloads..."
strands-metrics --db-path "$DB_PATH" sync-downloads --config-path "$CONFIG_DIR/packages.yaml"

echo "[sync-all] Loading goals configuration..."
strands-metrics --db-path "$DB_PATH" load-goals "$CONFIG_DIR/goals.yaml"

echo "[sync-all] Loading team configuration..."
strands-metrics --db-path "$DB_PATH" load-team "$CONFIG_DIR/team.yaml"

echo "[sync-all] Sync complete."
