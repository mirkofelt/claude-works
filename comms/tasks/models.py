from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    id: int | None
    message_id: int | None
    chat_id: int
    user_id: int
    content: str
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0
    created_at: int = 0
    started_at: int | None = None
    completed_at: int | None = None
    agent_id: str | None = None
    result: str | None = None
    error: str | None = None
    context_tokens: int = 0


@dataclass
class IncomingMessage:
    telegram_message_id: int
    chat_id: int
    from_user_id: int
    text: str | None
    voice_file_id: str | None
    timestamp: int
    is_edited: bool = False
