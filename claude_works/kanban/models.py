from dataclasses import dataclass
from enum import Enum


class Lane(str, Enum):
    BACKLOG = "backlog"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


class AgentClass(str, Enum):
    CHIEF = "chief"
    CONTROLLER = "controller"
    PO = "po"
    GENERALIST = "generalist"
    RESEARCHER = "researcher"
    CODER = "coder"
    MEMORY = "memory"
    MECHANIC = "mechanic"
    SECURITY = "security"


@dataclass
class KanbanTask:
    id: int | None
    chat_id: int
    user_id: int
    content: str
    lane: Lane = Lane.BACKLOG
    agent_class: AgentClass | None = None
    agent_id: str | None = None
    parent_id: int | None = None
    priority: int = 0
    created_at: int = 0
    assigned_at: int | None = None
    started_at: int | None = None
    completed_at: int | None = None
    result: str | None = None
    error: str | None = None
    message_id: int | None = None
