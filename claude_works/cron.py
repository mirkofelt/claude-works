"""Durable cron scheduler for the daemon.

Job handlers are registered in code; per-job state persists in the
``cron_jobs`` table (/data/claude-works.db) — survives restarts, no expiry.
Enable/disable and intervals are controlled via daemon_config::

    "cron": {
        "deploy_watch": {"enabled": true, "interval_minutes": 5, ...}
    }

Error policy: a failing job never silently retry-loops into the void.
Every new error text is pushed to the admins once; repeats of the same
error are logged but not re-notified.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiosqlite

from . import config

logger = logging.getLogger(__name__)

TICK_SECONDS = 20.0


@dataclass
class CronContext:
    """Passed to every job handler invocation."""
    conn: aiosqlite.Connection
    job_cfg: dict
    notify: Callable[[str], Awaitable[None]]          # message → all admins
    save_state: Callable[[dict], Awaitable[None]]     # persist state mid-run


@dataclass
class CronJob:
    name: str
    handler: Callable[[CronContext, dict], Awaitable[dict | None]]
    default_interval_seconds: int = 300
    default_enabled: bool = False


class CronManager:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        notify: Callable[[str], Awaitable[None]],
        is_running: Callable[[], bool],
    ) -> None:
        self._conn = conn
        self._notify = notify
        self._is_running = is_running
        self._jobs: dict[str, CronJob] = {}

    def register(self, job: CronJob) -> None:
        self._jobs[job.name] = job

    # ── config resolution ─────────────────────────────────────

    def _job_cfg(self, name: str) -> dict:
        try:
            cfg = config.section("cron").get(name, {})
        except Exception:
            cfg = {}
        return cfg if isinstance(cfg, dict) else {}

    def _enabled(self, job: CronJob) -> bool:
        return bool(self._job_cfg(job.name).get("enabled", job.default_enabled))

    def _interval(self, job: CronJob) -> int:
        cfg = self._job_cfg(job.name)
        if "interval_minutes" in cfg:
            return max(60, int(float(cfg["interval_minutes"]) * 60))
        if "interval_seconds" in cfg:
            return max(60, int(cfg["interval_seconds"]))
        return job.default_interval_seconds

    # ── persistence ───────────────────────────────────────────

    async def _ensure_rows(self) -> None:
        now = int(time.time())
        for job in self._jobs.values():
            await self._conn.execute(
                """INSERT OR IGNORE INTO cron_jobs
                   (name, interval_seconds, state_json, created_at, updated_at)
                   VALUES (?, ?, '{}', ?, ?)""",
                (job.name, job.default_interval_seconds, now, now),
            )
        await self._conn.commit()

    async def _load_row(self, name: str) -> aiosqlite.Row | None:
        async with self._conn.execute(
            "SELECT * FROM cron_jobs WHERE name = ?", (name,)
        ) as cur:
            return await cur.fetchone()

    async def _save_state(self, name: str, state: dict) -> None:
        await self._conn.execute(
            "UPDATE cron_jobs SET state_json = ?, updated_at = ? WHERE name = ?",
            (json.dumps(state), int(time.time()), name),
        )
        await self._conn.commit()

    async def _mark_run(self, name: str, status: str, error: str | None) -> None:
        now = int(time.time())
        await self._conn.execute(
            """UPDATE cron_jobs
               SET last_run_at = ?, last_status = ?, last_error = ?, updated_at = ?
               WHERE name = ?""",
            (now, status, error, now, name),
        )
        await self._conn.commit()

    async def status(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for job in self._jobs.values():
            row = await self._load_row(job.name)
            out.append({
                "name": job.name,
                "enabled": self._enabled(job),
                "interval_seconds": self._interval(job),
                "last_run_at": row["last_run_at"] if row else None,
                "last_status": row["last_status"] if row else None,
                "last_error": row["last_error"] if row else None,
            })
        return out

    # ── scheduler loop ────────────────────────────────────────

    async def run(self) -> None:
        await self._ensure_rows()
        logger.info("Cron scheduler started (%d job(s): %s)",
                    len(self._jobs), ", ".join(self._jobs) or "-")
        try:
            while self._is_running():
                for job in list(self._jobs.values()):
                    try:
                        await self._tick_job(job)
                    except Exception:
                        logger.exception("Cron tick failed for job %s", job.name)
                await asyncio.sleep(TICK_SECONDS)
        except asyncio.CancelledError:
            pass

    async def _tick_job(self, job: CronJob) -> None:
        if not self._enabled(job):
            return
        row = await self._load_row(job.name)
        if row is None:
            await self._ensure_rows()
            row = await self._load_row(job.name)
            if row is None:
                return
        last_run = row["last_run_at"] or 0
        if time.time() - last_run < self._interval(job):
            return
        await self._run_job(job, row)

    async def _run_job(self, job: CronJob, row: aiosqlite.Row) -> None:
        try:
            state = json.loads(row["state_json"] or "{}")
        except json.JSONDecodeError:
            state = {}

        ctx = CronContext(
            conn=self._conn,
            job_cfg=self._job_cfg(job.name),
            notify=self._notify,
            save_state=lambda s, _n=job.name: self._save_state(_n, s),
        )
        try:
            new_state = await job.handler(ctx, state)
            if isinstance(new_state, dict):
                await self._save_state(job.name, new_state)
            await self._mark_run(job.name, "ok", None)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:500]
            logger.error("Cron job %s failed: %s", job.name, err)
            prev_err = row["last_error"]
            await self._mark_run(job.name, "error", err)
            if err != prev_err:  # notify once per distinct error, no spam loop
                try:
                    await self._notify(f"⚠️ Cron-Job '{job.name}' fehlgeschlagen:\n{err}")
                except Exception:
                    logger.exception("Cron error notification failed")
