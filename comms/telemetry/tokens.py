import logging
import time

import aiosqlite

from ..config import downgrade_model, estimate_cost, section

logger = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    pass


class TokenTracker:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def log(
        self,
        *,
        agent_id: str,
        agent_class: str,
        task_id: int | None,
        user_id: int | None,
        chat_id: int | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        now = int(time.time())
        cost = estimate_cost(model, input_tokens, output_tokens)
        await self._conn.execute(
            """INSERT INTO token_usage
               (agent_id, agent_class, task_id, user_id, chat_id, model,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                cost_usd, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, agent_class, task_id, user_id, chat_id, model,
             input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
             cost, now),
        )
        await self._conn.commit()
        logger.debug(
            "Tokens %s[%s] task=%s in=%d out=%d cost=$%.6f",
            agent_class, agent_id, task_id, input_tokens, output_tokens, cost,
        )

    async def _sum_cost(self, since: int) -> float:
        async with self._conn.execute(
            "SELECT SUM(cost_usd) FROM token_usage WHERE timestamp >= ?", (since,)
        ) as cur:
            row = await cur.fetchone()
        return float(row[0] or 0.0) if row else 0.0

    async def total_cost(self, since: int | None = None) -> float:
        if since is None:
            async with self._conn.execute("SELECT SUM(cost_usd) FROM token_usage") as cur:
                row = await cur.fetchone()
        else:
            async with self._conn.execute(
                "SELECT SUM(cost_usd) FROM token_usage WHERE timestamp >= ?", (since,)
            ) as cur:
                row = await cur.fetchone()
        return float(row[0] or 0.0) if row else 0.0

    async def get_allowed_model(self, requested_model: str) -> str | None:
        """Return model to use, or None to reject.

        Checks daily/monthly spending limits. If over limit:
          on_limit_exceeded=reject (default) → returns None → caller raises BudgetExceededError
          on_limit_exceeded=downgrade → returns next cheaper model (None if already cheapest)
        """
        cfg = section("spending")
        max_daily = cfg.get("max_daily_usd")
        max_monthly = cfg.get("max_monthly_usd")

        if max_daily is None and max_monthly is None:
            return requested_model

        now = int(time.time())
        over_budget = False

        if max_daily is not None and await self._sum_cost(now - 86400) >= max_daily:
            over_budget = True

        if not over_budget and max_monthly is not None:
            if await self._sum_cost(now - 2592000) >= max_monthly:
                over_budget = True

        if not over_budget:
            return requested_model

        on_exceed = cfg.get("on_limit_exceeded", "reject")
        if on_exceed == "downgrade":
            cheaper = downgrade_model(requested_model)
            logger.warning(
                "Budget exceeded — downgrading %s → %s", requested_model, cheaper or "reject"
            )
            return cheaper  # None if already cheapest tier → reject
        logger.warning("Budget exceeded — rejecting model %s", requested_model)
        return None

    async def stats(self, since: int | None = None) -> dict:
        where = "WHERE timestamp >= ?" if since else ""
        params: tuple = (since,) if since else ()
        async with self._conn.execute(
            f"""SELECT agent_class,
                       SUM(input_tokens) as input_total,
                       SUM(output_tokens) as output_total,
                       SUM(cache_read_tokens) as cache_read_total,
                       SUM(cache_write_tokens) as cache_write_total,
                       SUM(cost_usd) as cost_total,
                       COUNT(*) as calls
                FROM token_usage {where}
                GROUP BY agent_class""",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return {
            r["agent_class"]: {
                "input": r["input_total"] or 0,
                "output": r["output_total"] or 0,
                "cache_read": r["cache_read_total"] or 0,
                "cache_write": r["cache_write_total"] or 0,
                "cost_usd": round(r["cost_total"] or 0.0, 6),
                "calls": r["calls"] or 0,
            }
            for r in rows
        }

    async def totals(self, since: int | None = None) -> dict:
        where = "WHERE timestamp >= ?" if since else ""
        params: tuple = (since,) if since else ()
        async with self._conn.execute(
            f"""SELECT SUM(input_tokens) as input_total,
                       SUM(output_tokens) as output_total,
                       SUM(cache_read_tokens) as cache_read_total,
                       SUM(cache_write_tokens) as cache_write_total,
                       SUM(cost_usd) as cost_total,
                       COUNT(*) as calls
                FROM token_usage {where}""",
            params,
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "calls": 0}
        return {
            "input": row["input_total"] or 0,
            "output": row["output_total"] or 0,
            "cache_read": row["cache_read_total"] or 0,
            "cache_write": row["cache_write_total"] or 0,
            "cost_usd": round(row["cost_total"] or 0.0, 6),
            "calls": row["calls"] or 0,
        }

    async def timeseries(self, since: int, bucket_seconds: int = 3600) -> list[dict]:
        async with self._conn.execute(
            """SELECT (timestamp / ?) * ? as bucket,
                      agent_class,
                      SUM(input_tokens + output_tokens) as total_tokens,
                      SUM(cost_usd) as total_cost
               FROM token_usage
               WHERE timestamp >= ?
               GROUP BY bucket, agent_class
               ORDER BY bucket ASC""",
            (bucket_seconds, bucket_seconds, since),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "bucket": r["bucket"],
                "agent_class": r["agent_class"],
                "tokens": r["total_tokens"],
                "cost_usd": round(r["total_cost"] or 0.0, 6),
            }
            for r in rows
        ]
