import logging
import re
import time
from typing import Any

from ..kanban.models import KanbanTask
from ..tasks.tags import collect_output_tags, TagCollection
from ..telegram.renderer import md_to_html as _md_to_telegram_html

logger = logging.getLogger(__name__)

_ECHOED_TOOL_RE = re.compile(
    r"GitHub\s+(?:GET|POST|PUT|PATCH|DELETE)\s+[^\n]+:\n\s*[\[\{][\s\S]*?(?=\n[^\s\[\{]|\Z)",
    re.MULTILINE,
)
_MIN_ECHO_LINE_CHARS = 24
_STRUCTURAL_LINE_RE = re.compile(r'^(?:[{}\[\],]+|"[\w-]+":.*)$')


def _strip_echoed_tool_results(text: str) -> str:
    return _ECHOED_TOOL_RE.sub("[tool output stripped]", text).strip()


def strip_echoed_payloads(text: str, payloads: list[str]) -> str:
    """Strip tool-output / fetched content the agent echoed verbatim into its reply."""
    if not text or not payloads:
        return _strip_echoed_tool_results(text)

    echo_lines: set[str] = set()
    echo_lines_short: set[str] = set()
    for p in payloads:
        block = p.strip()
        if len(block) >= _MIN_ECHO_LINE_CHARS and block in text:
            text = text.replace(block, "")
        for ln in p.splitlines():
            s = ln.strip()
            if not s:
                continue
            if len(s) >= _MIN_ECHO_LINE_CHARS:
                echo_lines.add(s)
            else:
                echo_lines_short.add(s)

    def _is_echo(line: str) -> bool:
        s = line.strip()
        if s in echo_lines:
            return True
        if s in echo_lines_short and _STRUCTURAL_LINE_RE.match(s):
            return True
        return False

    if echo_lines or echo_lines_short:
        kept = [ln for ln in text.splitlines() if not _is_echo(ln)]
        text = "\n".join(kept)

    text = re.sub(r"\n{3,}", "\n\n", text)
    return _strip_echoed_tool_results(text)


async def on_agent_result(daemon: Any, task: KanbanTask, result: str | None, error: str | None = None) -> None:
    if task.id in daemon._chat_task_ids:
        daemon._chat_task_ids.discard(task.id)
        daemon._stop_typing(task.chat_id)
        daemon._flush_chat_queue(task.chat_id)
    reaction_info = daemon._pending_reactions.pop(task.id, None) if task.id else None
    if reaction_info:
        try:
            await daemon._api.set_message_reaction(reaction_info[0], reaction_info[1], None)
        except Exception:
            pass
        try:
            await daemon._conn.execute("DELETE FROM pending_reactions WHERE task_id = ?", (task.id,))
            await daemon._conn.commit()
        except Exception:
            pass
    reply_to_id: int | None = (
        reaction_info[1] if reaction_info else daemon._chat_reply_to.pop(task.id, None)
    )

    if task.parent_id is not None:
        if result and daemon._board:
            preview = result[:300] + ("…" if len(result) > 300 else "")
            try:
                label = task.content[:60] if task.content else f"Task {task.id}"
                await daemon._api.send_message(
                    task.chat_id,
                    f"✓ **Sub-Task**: {label}\n{preview}",
                )
            except Exception:
                pass
            try:
                siblings = await daemon._board.subtasks(task.parent_id)
                terminal = {"done", "failed", "blocked"}
                all_done = all(s.lane in terminal for s in siblings)
                if all_done and siblings:
                    results_text = "\n\n".join(
                        f"### Sub-Task {i+1}: {s.content[:80]}\n{(s.result or s.error or '(no result)')[:600]}"
                        for i, s in enumerate(siblings)
                    )
                    synth_desc = (
                        f"[Synthesize ORCHESTRATE results]\n\n"
                        f"All sub-tasks for parent task {task.parent_id} are complete.\n"
                        f"Synthesize the following results into a coherent summary for the user:\n\n"
                        f"{results_text}"
                    )
                    synth_proto = KanbanTask(
                        id=None, chat_id=task.chat_id, user_id=task.user_id,
                        content=synth_desc,
                    )
                    await daemon._board.push(synth_proto)
                    logger.info(
                        "All %d sub-tasks of parent %d done — synthesis task spawned",
                        len(siblings), task.parent_id,
                    )
            except Exception as e:
                logger.warning("Sub-task synthesis check failed for parent=%d: %s", task.parent_id, e)
        return

    echoed_payloads = daemon._recent_tool_payloads.pop(task.chat_id, [])
    if result:
        allowed = await daemon._security.check(
            result, task_id=task.id, chat_id=task.chat_id, user_id=task.user_id
        )
        if not allowed:
            await daemon._api.send_message(task.chat_id, "Response blocked by security policy.")
            return
        _tc: TagCollection = collect_output_tags(result)
        clean_result = _tc.clean_result
        keyboard = _tc.keyboard

        reply_markup = {"inline_keyboard": keyboard} if keyboard is not None else None
        initial_msg_id = daemon._pending_initial_msgs.pop(task.id, None) if task.id else None
        if initial_msg_id and task.id:
            try:
                await daemon._conn.execute("DELETE FROM pending_initial_msgs WHERE task_id = ?", (task.id,))
                await daemon._conn.commit()
            except Exception:
                pass

        clean_result = strip_echoed_payloads(clean_result, echoed_payloads)

        if clean_result.strip():
            html_result = _md_to_telegram_html(clean_result)
            if initial_msg_id:
                try:
                    await daemon._api.edit_message(task.chat_id, initial_msg_id, html_result, parse_mode="HTML", reply_markup=reply_markup)
                    sent = {"message_id": initial_msg_id}
                except Exception:
                    try:
                        sent = await daemon._api.send_message(task.chat_id, html_result, parse_mode="HTML", reply_markup=reply_markup, reply_to_message_id=reply_to_id)
                    except Exception:
                        sent = await daemon._api.send_message(task.chat_id, clean_result, reply_markup=reply_markup, reply_to_message_id=reply_to_id)
            else:
                try:
                    sent = await daemon._api.send_message(task.chat_id, html_result, parse_mode="HTML", reply_markup=reply_markup, reply_to_message_id=reply_to_id)
                except Exception:
                    logger.warning("HTML send failed for task=%d, retrying plain", task.id)
                    sent = await daemon._api.send_message(task.chat_id, clean_result, reply_markup=reply_markup, reply_to_message_id=reply_to_id)
        else:
            if initial_msg_id:
                try:
                    await daemon._api.edit_message(task.chat_id, initial_msg_id, "✓")
                except Exception:
                    pass
            sent = {"message_id": initial_msg_id or 0}

        await daemon._execute_output_tags(task, _tc, sent["message_id"])

    elif error:
        init_msg_id = daemon._pending_initial_msgs.pop(task.id, None) if task.id else None
        if init_msg_id and task.id:
            try:
                await daemon._conn.execute("DELETE FROM pending_initial_msgs WHERE task_id = ?", (task.id,))
                await daemon._conn.commit()
            except Exception:
                pass
        if "CLI_AUTH_REQUIRED" in error:
            err_text = "Claude CLI not logged in. Send /reauth to authenticate."
        else:
            err_text = None
            logger.debug("Agent error for task=%s (recovery will handle user notification): %s", task.id, error)
        if init_msg_id:
            notice = err_text or "⚠ Task failed — see logs."
            try:
                await daemon._api.edit_message(task.chat_id, init_msg_id, notice)
            except Exception:
                if err_text:
                    await daemon._api.send_message(task.chat_id, err_text)
        elif err_text:
            await daemon._api.send_message(task.chat_id, err_text)
