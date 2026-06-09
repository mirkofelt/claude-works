#!/bin/sh
set -e

DATA_DIR=/data
LOG_DIR="$DATA_DIR/logs"
INIT_LOG="$LOG_DIR/init.log"

mkdir -p "$LOG_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] container start" >> "$INIT_LOG"

# Install user-defined extra packages (documents container customisations in /data)
if [ -f "$DATA_DIR/requirements.local.txt" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] installing $DATA_DIR/requirements.local.txt" >> "$INIT_LOG"
    pip install --no-cache-dir -r "$DATA_DIR/requirements.local.txt" >> "$INIT_LOG" 2>&1
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done requirements.local.txt" >> "$INIT_LOG"
fi

# Run user init script (apt installs, env setup, tool downloads, etc.)
if [ -f "$DATA_DIR/init.sh" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] running $DATA_DIR/init.sh" >> "$INIT_LOG"
    sh "$DATA_DIR/init.sh" >> "$INIT_LOG" 2>&1
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done init.sh" >> "$INIT_LOG"
fi

exec "$@"
