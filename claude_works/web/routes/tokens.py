import json as _json
import time

from fastapi import APIRouter, Depends

from ... import config
from ..deps import get_conn, verify_token

router = APIRouter()


@router.get("/api/tokens", dependencies=[Depends(verify_token)])
async def get_tokens(period: str = "24h"):
    periods = {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}
    seconds = periods.get(period, 86400)
    since = int(time.time()) - seconds

    conn = await get_conn()
    async with conn.execute(
        """SELECT agent_class,
                  SUM(input_tokens) as input_total,
                  SUM(output_tokens) as output_total,
                  SUM(cache_read_tokens) as cache_read_total,
                  SUM(cache_write_tokens) as cache_write_total,
                  SUM(cost_usd) as cost_total,
                  COUNT(*) as calls
           FROM token_usage WHERE timestamp >= ?
           GROUP BY agent_class""",
        (since,),
    ) as cur:
        stat_rows = await cur.fetchall()

    bucket_seconds = 3600 if seconds <= 86400 else 21600
    async with conn.execute(
        """SELECT (timestamp / ?) * ? as bucket,
                  agent_class,
                  SUM(input_tokens + output_tokens) as total_tokens,
                  SUM(cost_usd) as total_cost
           FROM token_usage WHERE timestamp >= ?
           GROUP BY bucket, agent_class
           ORDER BY bucket ASC""",
        (bucket_seconds, bucket_seconds, since),
    ) as cur:
        ts_rows = await cur.fetchall()

    async with conn.execute(
        """SELECT source,
                  SUM(input_tokens) as input_total,
                  SUM(output_tokens) as output_total,
                  SUM(cache_read_tokens) as cache_read_total,
                  SUM(cache_write_tokens) as cache_write_total,
                  SUM(cost_usd) as cost_total,
                  COUNT(*) as calls,
                  COUNT(DISTINCT run_id) as runs
           FROM token_usage WHERE timestamp >= ?
           GROUP BY source""",
        (since,),
    ) as cur:
        source_rows = await cur.fetchall()

    async with conn.execute(
        """SELECT source, run_id, task_id,
                  SUM(input_tokens) as input_total,
                  SUM(output_tokens) as output_total,
                  SUM(cache_read_tokens) as cache_read_total,
                  SUM(cache_write_tokens) as cache_write_total,
                  SUM(cost_usd) as cost_total,
                  COUNT(*) as calls,
                  MIN(timestamp) as first_ts,
                  MAX(timestamp) as last_ts,
                  GROUP_CONCAT(DISTINCT model) as models
           FROM token_usage
           WHERE timestamp >= ? AND source != 'main_loop' AND run_id IS NOT NULL
           GROUP BY source, run_id
           ORDER BY last_ts DESC
           LIMIT 200""",
        (since,),
    ) as cur:
        run_rows = await cur.fetchall()

    billing_since = int(time.time()) - 2592000
    async with conn.execute(
        """SELECT sampled_at, session_pct, weekly_all_pct, weekly_sonnet_pct,
                  session_reset_at, weekly_reset_at, tokens_used, tokens_limit,
                  weekly_models_json
           FROM usage_snapshots WHERE sampled_at >= ?
           ORDER BY sampled_at ASC""",
        (billing_since,),
    ) as cur:
        snap_rows = await cur.fetchall()

    await conn.close()

    cli_usage_ts = [
        {
            "sampled_at": r["sampled_at"],
            "session_pct": r["session_pct"],
            "weekly_all_pct": r["weekly_all_pct"],
            "weekly_models": _json.loads(r["weekly_models_json"]) if r["weekly_models_json"] else [],
            "session_reset_at": r["session_reset_at"],
            "weekly_reset_at": r["weekly_reset_at"],
            "tokens_used": r["tokens_used"],
            "tokens_limit": r["tokens_limit"],
        }
        for r in snap_rows
    ]

    stats = {
        r["agent_class"]: {
            "input": r["input_total"] or 0,
            "output": r["output_total"] or 0,
            "cache_read": r["cache_read_total"] or 0,
            "cache_write": r["cache_write_total"] or 0,
            "cost_usd": round(r["cost_total"] or 0.0, 6),
            "calls": r["calls"] or 0,
        }
        for r in stat_rows
    }
    total_cost = sum(v["cost_usd"] for v in stats.values())
    timeseries = [
        {
            "bucket": r["bucket"],
            "agent_class": r["agent_class"],
            "tokens": r["total_tokens"],
            "cost_usd": round(r["total_cost"] or 0.0, 6),
        }
        for r in ts_rows
    ]
    by_source = {
        r["source"]: {
            "input": r["input_total"] or 0,
            "output": r["output_total"] or 0,
            "cache_read": r["cache_read_total"] or 0,
            "cache_write": r["cache_write_total"] or 0,
            "cost_usd": round(r["cost_total"] or 0.0, 6),
            "calls": r["calls"] or 0,
            "runs": r["runs"] or 0,
        }
        for r in source_rows
    }
    runs = [
        {
            "source": r["source"],
            "run_id": r["run_id"],
            "task_id": r["task_id"],
            "input": r["input_total"] or 0,
            "output": r["output_total"] or 0,
            "cache_read": r["cache_read_total"] or 0,
            "cache_write": r["cache_write_total"] or 0,
            "total": (r["input_total"] or 0) + (r["output_total"] or 0)
            + (r["cache_read_total"] or 0) + (r["cache_write_total"] or 0),
            "cost_usd": round(r["cost_total"] or 0.0, 6),
            "calls": r["calls"] or 0,
            "first_ts": r["first_ts"],
            "last_ts": r["last_ts"],
            "models": (r["models"] or "").split(",") if r["models"] else [],
        }
        for r in run_rows
    ]
    return {
        "period": period,
        "stats": stats,
        "by_source": by_source,
        "runs": runs,
        "total_cost_usd": round(total_cost, 6),
        "timeseries": timeseries,
        "cli_usage": cli_usage_ts,
        "is_cli": True,
    }


@router.get("/api/tokens/run", dependencies=[Depends(verify_token)])
async def get_token_run(run_id: str):
    """Drill-down: every individual API call belonging to one run_id."""
    conn = await get_conn()
    async with conn.execute(
        """SELECT id, agent_id, agent_class, source, model, task_id,
                  input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                  cost_usd, timestamp
           FROM token_usage WHERE run_id = ?
           ORDER BY timestamp ASC, id ASC""",
        (run_id,),
    ) as cur:
        rows = await cur.fetchall()
    await conn.close()
    return {
        "run_id": run_id,
        "calls": [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "agent_class": r["agent_class"],
                "source": r["source"],
                "model": r["model"],
                "task_id": r["task_id"],
                "input": r["input_tokens"] or 0,
                "output": r["output_tokens"] or 0,
                "cache_read": r["cache_read_tokens"] or 0,
                "cache_write": r["cache_write_tokens"] or 0,
                "total": (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
                + (r["cache_read_tokens"] or 0) + (r["cache_write_tokens"] or 0),
                "cost_usd": round(r["cost_usd"] or 0.0, 6),
                "timestamp": r["timestamp"],
            }
            for r in rows
        ],
    }
