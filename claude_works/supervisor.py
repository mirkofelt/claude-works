"""
Supervisor: starts and monitors the claude-works daemon.
Lightweight — no business logic. Spawn, check health, restart on failure, alert on repeated failure.
"""
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request

logger = logging.getLogger(__name__)

HEALTH_URL = "http://localhost:8080/health"
HEALTH_INTERVAL = int(os.environ.get("HEALTH_INTERVAL", "60"))
PID_FILE = "/data/supervisor.pid"
DAEMON_CMD = [sys.executable, "-m", "claude_works.main"]


def _load_settings() -> dict:
    path = os.environ.get("SETTINGS_FILE", "/data/settings.json")
    with open(path) as f:
        return json.load(f)


def _send_telegram_alert(message: str) -> None:
    try:
        settings = _load_settings()
        tg = settings.get("telegram", {})
        token = tg.get("token")
        admin_ids = settings.get("users", {}).get("admin_ids") or tg.get("admin_chat_ids", [])
        if not token or not admin_ids:
            return
        for chat_id in admin_ids:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            body = json.dumps({"chat_id": chat_id, "text": message}).encode()
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=10):
                pass
    except Exception as e:
        logger.error("Alert send failed: %s", e)


def _check_health() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("status") == "ok"
    except Exception:
        return False


class Supervisor:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._running = False
        self._restart_count = 0
        self._last_restart = 0.0

    def _get_backoff(self) -> int:
        try:
            settings = _load_settings()
            backoffs = settings.get("supervisor", {}).get("restart_backoff_seconds", [5, 15, 60])
        except Exception:
            backoffs = [5, 15, 60]
        idx = min(self._restart_count, len(backoffs) - 1)
        return backoffs[idx]

    def _max_restarts(self) -> int:
        try:
            return _load_settings().get("supervisor", {}).get("max_restart_attempts", 3)
        except Exception:
            return 3

    def start_daemon(self) -> None:
        logger.info("Starting claude-works daemon (attempt %d)", self._restart_count + 1)
        self._proc = subprocess.Popen(
            DAEMON_CMD,
            env=os.environ.copy(),
        )
        self._last_restart = time.time()

    def daemon_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def run(self) -> None:
        self._running = True
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))

        self.start_daemon()

        while self._running:
            await asyncio.sleep(HEALTH_INTERVAL)
            if not self.daemon_alive():
                returncode = self._proc.returncode if self._proc else -1
                logger.warning("Daemon exited (code %d)", returncode)
                self._restart_count += 1

                if self._restart_count > self._max_restarts():
                    msg = f"claude-works daemon failed {self._restart_count} times. Manual intervention required."
                    logger.error(msg)
                    _send_telegram_alert(f"⛔ {msg}")
                    self._running = False
                    break

                backoff = self._get_backoff()
                logger.info("Restarting in %ds...", backoff)
                _send_telegram_alert(f"⚠️ claude-works daemon restarting (attempt {self._restart_count})")
                await asyncio.sleep(backoff)
                self.start_daemon()
                continue

            if not _check_health():
                logger.warning("Health check failed — daemon may be stuck")
                _send_telegram_alert("⚠️ claude-works health check failed")

        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()

        if os.path.exists(PID_FILE):
            os.unlink(PID_FILE)

    def shutdown(self) -> None:
        self._running = False


async def main() -> None:
    try:
        from claude_works.logging_setup import setup as _setup_logging
        _setup_logging()
    except Exception:
        logging.basicConfig(
            level=os.environ.get("LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    supervisor = Supervisor()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, supervisor.shutdown)
    await supervisor.run()


if __name__ == "__main__":
    asyncio.run(main())
