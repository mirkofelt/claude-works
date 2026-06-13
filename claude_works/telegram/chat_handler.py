import asyncio
import logging
import time
from typing import Any

from .. import config, db
from ..agents.heartbeat import run_with_heartbeat
from ..agents.specialist.generalist import GeneralistAgent
from ..auth import trust as trust_mod
from ..kanban.board import build_offload_content, is_offloaded
from ..kanban.models import KanbanTask
from ..knowledge import store as knowledge_store
from ..llm.errors import RateLimitError
from ..tasks import tags as _tags
from .renderer import md_to_html as _md_to_telegram_html

logger = logging.getLogger(__name__)

_LONG_RUN_NOTICE_SECONDS = 60.0


def _user_error(context: str, exc: Exception | None = None) -> str:
    if exc is not None:
        logger.warning("%s: %s", context, exc)
    _FRIENDLY: dict[type, str] = {
        asyncio.TimeoutError: "Timed out.",
    }
    if exc is not None:
        for exc_type, msg in _FRIENDLY.items():
            if isinstance(exc, exc_type):
                return f"⚠️ {context} — {msg}"
    return f"⚠️ {context}."


async def long_run_notice(daemon: Any, chat_id: int) -> None:
    """Send a single status message when an inline run exceeds the notice delay."""
    try:
        await asyncio.sleep(_LONG_RUN_NOTICE_SECONDS)
        await daemon._api.send_message(chat_id, "⏳ Still working on it.")
        logger.info("Chat %d: long-run notice sent after %.0fs", chat_id, _LONG_RUN_NOTICE_SECONDS)
    except asyncio.CancelledError:
        raise


async def offload_after_timeout(
    daemon: Any, chat_id: int, user_id: int, content: str, task_id: int | None, elapsed: float
) -> None:
    """Inline run hit timeout: hand the job to the kanban board instead of killing it."""
    if not daemon._board or is_offloaded(content):
        if task_id and daemon._board:
            await daemon._board.fail(task_id, f"timeout ({elapsed:.0f}s) — already offloaded, no retry")
        logger.warning("Chat %d: timeout after %.0fs, no offload (board=%s, marked=%s)",
                       chat_id, elapsed, bool(daemon._board), is_offloaded(content))
        await daemon._api.send_message(chat_id, "Timed out. Retry also failed.")
        return

    offload_content = build_offload_content(content, elapsed)
    offloaded = False
    if task_id:
        offloaded = await daemon._board.offload(task_id, offload_content)
    if not offloaded:
        await daemon._board.push(
            KanbanTask(id=None, chat_id=chat_id, user_id=user_id, content=offload_content)
        )
    logger.info(
        "Chat %d: inline run timed out after %.0fs — offloaded to board (task=%s)",
        chat_id, elapsed, task_id,
    )
    await daemon._api.send_message(
        chat_id, "Taking longer. Running in the background — will notify when done."
    )


async def handle_chat(daemon: Any, chat_id: int, user_id: int, content: str, reply_to_msg_id: int | None = None) -> None:
    """Handle a conversational message directly, bypassing kanban controller."""
    daemon._start_typing(chat_id)
    task_id: int | None = None
    reply_timeout = config.agent_timeout("reply_timeout_seconds")
    idle_timeout = config.agent_timeout("idle_timeout_seconds")
    run_started = time.monotonic()
    notice_task = asyncio.create_task(long_run_notice(daemon, chat_id))
    try:
        agent = daemon._chat_agents.get(chat_id)
        if agent is None:
            persona = daemon._user_personas.get(user_id, "")
            if not persona and daemon._coordinator and daemon._coordinator._chief:
                persona = daemon._coordinator._chief.persona
            grp = config.group_config(chat_id)
            if grp.get("persona"):
                persona = grp["persona"]
            user_context = {
                "user_id": user_id,
                "chat_id": chat_id,
                "background": daemon._user_backgrounds.get(user_id, ""),
                "is_group": chat_id < 0,
            }
            if grp.get("focus"):
                user_context["focus"] = grp["focus"]
            if grp.get("communication_style"):
                user_context["communication_style"] = grp["communication_style"]
            provider = daemon._coordinator._get_provider() if daemon._coordinator else None
            agent = GeneralistAgent(
                task_id=0,
                user_context=user_context,
                provider=provider,
                token_tracker=daemon._token_tracker,
                persona=persona,
                agent_class="chief",
            )
            history = await daemon._load_chat_history(chat_id)
            if history:
                agent._messages = history
                logger.info("Chat %d: restored %d history messages from DB", chat_id, len(history))
            daemon._chat_agents[chat_id] = agent
            daemon._chat_agent_fingerprints[chat_id] = daemon._chat_config_fingerprint(chat_id)
        if daemon._board:
            proto = KanbanTask(id=None, chat_id=chat_id, user_id=user_id, content=content)
            task_id = await daemon._board.push_active(proto, agent_id="chat")
            if task_id:
                daemon._chat_task_ids.add(task_id)
                if reply_to_msg_id:
                    daemon._chat_reply_to[task_id] = reply_to_msg_id
        deadline = run_started + reply_timeout
        enriched_content = content
        try:
            _kb_conn = await db.get_conn()
            _trust = await trust_mod.chat_trust(_kb_conn, chat_id, user_id)
            _kb_hits = await knowledge_store.search(_kb_conn, content, limit=5, trust=_trust)
            await _kb_conn.close()
            if _kb_hits:
                _kb_lines = []
                for _e in _kb_hits:
                    entry_tags = ", ".join(_e.get("tags") or [])
                    tag_str = f" [{entry_tags}]" if entry_tags else ""
                    _body = _e["content"][:500]
                    _kb_lines.append(f"- [{_e['type']}]{tag_str} **{_e['title']}**: {_body}")
                enriched_content = (
                    "## Relevant Knowledge Base Entries\n"
                    + "\n".join(_kb_lines)
                    + "\n\n---\n\n"
                    + content
                )
                logger.debug("Chat %d: injected %d KB entries as context", chat_id, len(_kb_hits))
        except Exception as _kb_err:
            logger.debug("Chat %d: KB auto-inject failed: %s", chat_id, _kb_err)

        result = await run_with_heartbeat(
            agent.run(enriched_content), agent.heartbeat, idle_timeout, deadline=deadline
        )
        preliminary_msg_id: int | None = None
        for _ in range(5):
            clean, tool_feedback = await daemon._exec_tool_tags(result, user_id=user_id, chat_id=chat_id)
            agent.heartbeat.beat()
            if not tool_feedback:
                result = clean
                break
            if preliminary_msg_id is None and clean.strip():
                try:
                    init = await daemon._api.send_message(
                        chat_id,
                        _md_to_telegram_html(clean) + "\n\n<i>✎ working...</i>",
                        parse_mode="HTML",
                        reply_to_message_id=reply_to_msg_id,
                    )
                    preliminary_msg_id = init["message_id"]
                    if task_id:
                        daemon._pending_initial_msgs[task_id] = preliminary_msg_id
                except Exception:
                    pass
            logger.info("Chat %d: tool results fed back, continuing", chat_id)
            result = await run_with_heartbeat(
                agent.run(f"[Tool results]\n{tool_feedback}\n\nContinue with the task."),
                agent.heartbeat, idle_timeout, deadline=deadline,
            )
        clean_result, board_task_desc = _tags.extract_board_task(result)
        if board_task_desc and daemon._board:
            board_proto = KanbanTask(id=None, chat_id=chat_id, user_id=user_id, content=board_task_desc)
            await daemon._board.push(board_proto)
            result = clean_result
            logger.info("Chat %d: agent self-routed task to board: %s", chat_id, board_task_desc[:80])

        if task_id and daemon._board:
            await daemon._board.complete(task_id, result[:2000] if result else "")
        real_task = KanbanTask(id=task_id, chat_id=chat_id, user_id=user_id, content=content)
        await daemon._on_agent_result(real_task, result, None)
    except asyncio.TimeoutError:
        await offload_after_timeout(
            daemon, chat_id, user_id, content, task_id, time.monotonic() - run_started
        )
    except RateLimitError as exc:
        wait = int(exc.retry_after or 30)
        if task_id and daemon._board:
            try:
                await daemon._board.fail(task_id, f"rate limited ({wait}s)")
            except Exception:
                pass
        logger.warning("Chat %d rate limited, retry_after=%ds", chat_id, wait)
        await daemon._api.send_message(chat_id, f"⏳ API rate limited — please retry in {wait}s.")
    except Exception as exc:
        if task_id and daemon._board:
            try:
                await daemon._board.fail(task_id, "exception in chat handler")
            except Exception:
                pass
        logger.exception("Chat handler error for chat=%d", chat_id)
        await daemon._api.send_message(chat_id, _user_error("Processing error", exc))
        daemon._chat_exception_count += 1
        if daemon._chat_exception_count >= 3:
            daemon._chat_exception_count = 0
            asyncio.ensure_future(daemon.trigger_repair(f"Chat handler crashed 3x: {exc}"))
    else:
        daemon._chat_exception_count = 0
    finally:
        notice_task.cancel()
        if task_id:
            daemon._chat_task_ids.discard(task_id)
        daemon._stop_typing(chat_id)
        daemon._flush_chat_queue(chat_id)
