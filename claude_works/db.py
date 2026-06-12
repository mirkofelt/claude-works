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
    trust_level INTEGER NOT NULL DEFAULT 2,
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
    source TEXT NOT NULL DEFAULT 'main_loop',
    run_id TEXT,
    timestamp INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_token_usage_time ON token_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_token_usage_class ON token_usage(agent_class, timestamp);
CREATE INDEX IF NOT EXISTS idx_token_usage_run ON token_usage(run_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_source ON token_usage(source, timestamp);

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL DEFAULT 'note',
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    source TEXT,
    user_id INTEGER,
    visibility INTEGER NOT NULL DEFAULT 0,
    origin_chat_id INTEGER,
    quarantined INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_type ON knowledge(type);
CREATE INDEX IF NOT EXISTS idx_knowledge_user ON knowledge(user_id, updated_at);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    title, content, tags,
    content='knowledge', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, COALESCE(new.tags,''));
END;

CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, COALESCE(old.tags,''));
END;

CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, COALESCE(old.tags,''));
    INSERT INTO knowledge_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, COALESCE(new.tags,''));
END;

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

CREATE TABLE IF NOT EXISTS admin_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    sent_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS daemon_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_reactions (
    task_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    tg_msg_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_initial_msgs (
    task_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    tg_msg_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tokens_used INTEGER,
    tokens_limit INTEGER,
    usage_pct REAL,
    sampled_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_snapshots_time ON usage_snapshots(sampled_at);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    msg TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, ts);

CREATE TABLE IF NOT EXISTS cron_jobs (
    name TEXT PRIMARY KEY,
    interval_seconds INTEGER NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    last_run_at INTEGER,
    last_status TEXT,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_types TEXT NOT NULL,
    content_preview TEXT,
    task_id INTEGER,
    chat_id INTEGER,
    decision TEXT NOT NULL,
    decided_by INTEGER,
    requested_at INTEGER NOT NULL,
    decided_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_log_time ON approval_log(decided_at DESC);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    remind_at INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    fired_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_reminders_pending ON reminders(remind_at) WHERE fired_at IS NULL;
"""

CONFIG_TABLES = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS daemon_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    settings_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS security_allowlist (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    always_allowed_actions TEXT NOT NULL DEFAULT '[]',
    skip_all INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
"""

_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN persona TEXT",
    "ALTER TABLE token_usage ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0",
    # Sub-agent attribution: source labels the subsystem (main_loop/coderteam/background),
    # run_id groups all API calls of one logical run. Existing rows default to main_loop.
    "ALTER TABLE token_usage ADD COLUMN source TEXT NOT NULL DEFAULT 'main_loop'",
    "ALTER TABLE token_usage ADD COLUMN run_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_token_usage_run ON token_usage(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_token_usage_source ON token_usage(source, timestamp)",
    "ALTER TABLE usage_snapshots ADD COLUMN usage_pct REAL",
    "ALTER TABLE usage_snapshots ADD COLUMN session_pct REAL",
    "ALTER TABLE usage_snapshots ADD COLUMN weekly_all_pct REAL",
    "ALTER TABLE usage_snapshots ADD COLUMN weekly_sonnet_pct REAL",
    "ALTER TABLE usage_snapshots ADD COLUMN session_reset_at INTEGER",
    "ALTER TABLE usage_snapshots ADD COLUMN weekly_reset_at INTEGER",
    "ALTER TABLE usage_snapshots ADD COLUMN weekly_models_json TEXT",
    # Trust levels: users.trust_level (0=owner/admin, 2=contact, 3=unknown),
    # knowledge.visibility (0=private/admin-only, 2=contacts, 3=public).
    "ALTER TABLE users ADD COLUMN trust_level INTEGER NOT NULL DEFAULT 2",
    "ALTER TABLE knowledge ADD COLUMN visibility INTEGER NOT NULL DEFAULT 0",
    # Backfill: lock down all pre-existing KB entries (defensive; ALTER default covers new col)
    "UPDATE knowledge SET visibility = 0 WHERE visibility IS NULL",
    # Admins are always effective level 0 (effective_trust maps role='admin' → 0);
    # backfill the column too so Web UI / raw queries show the truth. Idempotent.
    "UPDATE users SET trust_level = 0 WHERE role = 'admin' AND trust_level != 0",
    # Write-side trust gating: knowledge.origin_chat_id (woher kam der Eintrag),
    # knowledge.quarantined (1 = aus nicht vertrautem Chat, wartet auf Admin-Freigabe).
    "ALTER TABLE knowledge ADD COLUMN origin_chat_id INTEGER",
    "ALTER TABLE knowledge ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0",
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


_CONFIG_MIGRATIONS = [
    # Rename system.deploy_guard → system.claude_guard (service renamed).
    # json_set with json() embeds the value as a JSON object (not a string);
    # json_remove then drops the old key. Guard ensures idempotency.
    """UPDATE daemon_config
       SET settings_json = json_remove(
           json_set(
               settings_json,
               '$.system.claude_guard',
               json(json_extract(settings_json, '$.system.deploy_guard'))
           ),
           '$.system.deploy_guard'
       )
       WHERE id = 1
         AND json_extract(settings_json, '$.system.deploy_guard') IS NOT NULL
         AND json_extract(settings_json, '$.system.claude_guard') IS NULL""",
]


async def _apply_config_migrations(conn: aiosqlite.Connection) -> None:
    for sql in _CONFIG_MIGRATIONS:
        try:
            await conn.execute(sql)
            await conn.commit()
        except Exception:
            pass


async def init_config(db_path: str | None = None) -> aiosqlite.Connection:
    path = db_path or _config_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(CONFIG_TABLES)
    await conn.commit()
    await _apply_config_migrations(conn)
    return conn


async def get_conn(db_path: str | None = None) -> aiosqlite.Connection:
    return await init(db_path)
