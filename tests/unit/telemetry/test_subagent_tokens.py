import pytest
import aiosqlite

import claude_works.config as cfg
from claude_works.telemetry.tokens import TokenTracker
from claude_works.db import CREATE_TABLES


@pytest.fixture(autouse=True)
def _init_config(monkeypatch):
    # tracker.log → estimate_cost → config.section needs an initialised config.
    monkeypatch.setattr(cfg, "_settings", {})


async def _make_tracker() -> TokenTracker:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(CREATE_TABLES)
    await conn.commit()
    return TokenTracker(conn)


@pytest.mark.asyncio
async def test_log_defaults_to_main_loop():
    """A call without source/run_id is attributed to the main loop."""
    tracker = await _make_tracker()
    await tracker.log(
        agent_id="a1", agent_class="chief", task_id=1, user_id=1, chat_id=1,
        model="claude-sonnet-4-6", input_tokens=100, output_tokens=50,
    )
    async with tracker._conn.execute(
        "SELECT source, run_id FROM token_usage"
    ) as cur:
        row = await cur.fetchone()
    assert row["source"] == "main_loop"
    assert row["run_id"] is None


@pytest.mark.asyncio
async def test_subagent_calls_share_run_id():
    """Each API call is one row; all calls of a run share run_id and source."""
    tracker = await _make_tracker()
    run_id = "team42"
    # Simulate a 4-stage CodeTeam pipeline — one row PER call, not aggregated.
    for stage_in, stage_out in [(100, 10), (200, 80), (150, 60), (300, 120)]:
        await tracker.log(
            agent_id="m" + str(stage_in), agent_class="coder", task_id=7,
            user_id=1, chat_id=1, model="claude-sonnet-4-6",
            input_tokens=stage_in, output_tokens=stage_out,
            source="coderteam", run_id=run_id,
        )

    # Four distinct call rows persisted.
    async with tracker._conn.execute(
        "SELECT COUNT(*) AS n FROM token_usage WHERE run_id = ?", (run_id,)
    ) as cur:
        assert (await cur.fetchone())["n"] == 4

    # Run summary reconstructs via GROUP BY run_id.
    async with tracker._conn.execute(
        """SELECT source, task_id, COUNT(*) AS calls,
                  SUM(input_tokens) AS inp, SUM(output_tokens) AS out
           FROM token_usage WHERE run_id = ? GROUP BY run_id""",
        (run_id,),
    ) as cur:
        summary = await cur.fetchone()
    assert summary["source"] == "coderteam"
    assert summary["task_id"] == 7
    assert summary["calls"] == 4
    assert summary["inp"] == 750
    assert summary["out"] == 270


def test_codeteam_members_share_source_and_run_id():
    """Every CodeTeam member logs under the team's source and shared run_id."""
    from claude_works.agents.specialist.code_team import CodeTeam

    team = CodeTeam(task_id=5, token_tracker=None)
    assert team.source == "coderteam"
    assert team.run_id == team.id

    members = [team._member("addendum", stage) for stage in ("architect", "developer", "qa")]
    for m in members:
        assert m.source == "coderteam"
        assert m.run_id == team.id


def test_generalist_defaults_to_main_loop():
    """A plain agent defaults to main_loop with its own id as run_id."""
    from claude_works.agents.specialist.generalist import GeneralistAgent

    agent = GeneralistAgent(task_id=0, token_tracker=None)
    assert agent.source == "main_loop"
    assert agent.run_id == agent.id


@pytest.mark.asyncio
async def test_runs_query_excludes_main_loop():
    """The Token-tab runs query surfaces only sub-agent runs."""
    tracker = await _make_tracker()
    await tracker.log(
        agent_id="chat", agent_class="chief", task_id=1, user_id=1, chat_id=1,
        model="claude-sonnet-4-6", input_tokens=10, output_tokens=5,
    )
    await tracker.log(
        agent_id="bg1", agent_class="researcher", task_id=2, user_id=1, chat_id=1,
        model="claude-haiku", input_tokens=20, output_tokens=8,
        source="background", run_id="researcher-bg1",
    )
    async with tracker._conn.execute(
        """SELECT source, run_id, COUNT(*) AS calls FROM token_usage
           WHERE source != 'main_loop' AND run_id IS NOT NULL
           GROUP BY source, run_id"""
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "background"
    assert rows[0]["run_id"] == "researcher-bg1"
