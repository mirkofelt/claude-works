import aiosqlite
import os
from pathlib import Path


def _db_path() -> str:
    return os.environ.get("DB_FILE", "/data/claude-works.db")


def _config_db_path() -> str:
    return os.environ.get("CONFIG_DB_FILE", "/data/config.db")


CREATE_TABLES = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    telegram_id INTEGER UNIQUE NOT NULL,
    name TEXT,
    role TEXT NOT NULL DEFAULT 'blocked',
    created_at INTEGER NOT NULL,
    last_seen INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_message_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    from_user_id INTEGER,
    text TEXT,
    voice_file_id TEXT,
    timestamp INTEGER NOT NULL,
    UNIQUE(telegram_message_id, chat_id)
);

CREATE TABLE IF NOT EXISTS bot_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_message_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    task_id INTEGER,
    text TEXT,
    sent_at INTEGER NOT NULL,
    UNIQUE(telegram_message_id, chat_id)
);

CREATE TABLE IF NOT EXISTS reactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_message_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    from_user_id INTEGER NOT NULL,
    emoji TEXT NOT NULL,
    action TEXT,
    received_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    agent_id TEXT,
    result TEXT,
    error TEXT,
    context_tokens INTEGER DEFAULT 0,
    FOREIGN KEY(message_id) REFERENCES messages(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, status);

CREATE TABLE IF NOT EXISTS kanban_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    lane TEXT NOT NULL DEFAULT 'backlog',
    agent_class TEXT,
    agent_id TEXT,
    parent_id INTEGER,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    assigned_at INTEGER,
    started_at INTEGER,
    completed_at INTEGER,
    result TEXT,
    error TEXT,
    message_id INTEGER,
    FOREIGN KEY(parent_id) REFERENCES kanban_tasks(id),
    FOREIGN KEY(message_id) REFERENCES messages(id)
);

CREATE INDEX IF NOT EXISTS idx_kanban_lane ON kanban_tasks(lane);
CREATE INDEX IF NOT EXISTS idx_kanban_class ON kanban_tasks(lane, agent_class);
CREATE INDEX IF NOT EXISTS idx_kanban_user ON kanban_tasks(user_id, lane);

CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    agent_class TEXT NOT NULL,
    task_id INTEGER,
    user_id INTEGER,
    chat_id INTEGER,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    timestamp INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_token_usage_time ON token_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_token_usage_class ON token_usage(agent_class, timestamp);

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL DEFAULT 'note',
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    source TEXT,
    user_id INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_type ON knowledge(type);
CREATE INDEX IF NOT EXISTS idx_knowledge_user ON knowledge(user_id, updated_at);

CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    tags TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(user_id, key)
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id TEXT PRIMARY KEY,
    task_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    context_tokens INTEGER DEFAULT 0,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS security_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    action_types TEXT NOT NULL,
    content_preview TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at INTEGER NOT NULL,
    decided_at INTEGER,
    decided_by INTEGER
);
"""

CONFIG_TABLES = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS daemon_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    settings_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""

_MIGRATIONS = [
    "ALTER TABLE token_usage ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0",
]


async def _apply_migrations(conn: aiosqlite.Connection) -> None:
    for sql in _MIGRATIONS:
        try:
            await conn.execute(sql)
            await conn.commit()
        except Exception:
            pass  # column already exists


async def init(db_path: str | None = None) -> aiosqlite.Connection:
    path = db_path or _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(CREATE_TABLES)
    await conn.commit()
    await _apply_migrations(conn)
    return conn


async def init_config(db_path: str | None = None) -> aiosqlite.Connection:
    path = db_path or _config_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(CONFIG_TABLES)
    await conn.commit()
    return conn


async def get_conn(db_path: str | None = None) -> aiosqlite.Connection:
    return await init(db_path)
