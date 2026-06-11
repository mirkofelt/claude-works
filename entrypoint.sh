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

# Symlink /root/.claude → /data/.claude so claude CLI auth persists across restarts
# claude CLI stores credentials at ~/.claude; /data is the persistent volume
mkdir -p "$DATA_DIR/.claude"
if [ ! -L /root/.claude ]; then
    rm -rf /root/.claude
    ln -s "$DATA_DIR/.claude" /root/.claude
fi

# Start Tor daemon in background (provides SOCKS5 proxy at 127.0.0.1:9050)
if command -v tor >/dev/null 2>&1; then
    mkdir -p /var/lib/tor /run/tor
    chown -R root:root /var/lib/tor /run/tor
    tor --RunAsDaemon 1 \
        --DataDirectory /var/lib/tor \
        --PidFile /run/tor/tor.pid \
        --Log "warn file $LOG_DIR/tor.log" >> "$INIT_LOG" 2>&1
    TOR_EXIT=$?
    if [ $TOR_EXIT -ne 0 ]; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] tor failed to start (exit $TOR_EXIT)" >> "$INIT_LOG"
    else
        # Wait until Tor's SOCKS port is accepting connections (max 90s)
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] waiting for Tor bootstrap..." >> "$INIT_LOG"
        i=0
        while [ $i -lt 90 ]; do
            if (echo "" | nc -w1 127.0.0.1 9050) >/dev/null 2>&1; then
                echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Tor ready on :9050 (${i}s)" >> "$INIT_LOG"
                break
            fi
            sleep 1
            i=$((i + 1))
        done
        if [ $i -ge 90 ]; then
            echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Tor not ready after 90s (continuing anyway)" >> "$INIT_LOG"
        fi
    fi
fi

exec "$@"
