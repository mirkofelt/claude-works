# Claude Works — Architecture

## Process Structure

```
supervisor/supervisor.py     ← watchdog process (separate)
└── claude_works/main.py            ← Daemon (mode state machine)
    ├── ModeManager           ← daemon mode lifecycle (STARTUP→INITIALIZE|MIGRATE|RUN↔REPAIR)
    ├── TelegramPoller        ← long-poll loop (RUN mode only)
    ├── AgentCoordinator      ← multi-layer agent orchestration (RUN mode only)
    │   ├── ControllerAgent   ← LLM-based task routing
    │   ├── ChiefAgent        ← persona-aware strategy
    │   ├── ProductOwnerAgent ← project decomposition + child synthesis
    │   ├── MechanicAgent     ← MIGRATE + REPAIR handling (MIGRATE/REPAIR mode only)
    │   └── Specialist pool   ← generalist / researcher / code_team / memory
    ├── KanbanBoard           ← task lifecycle (backlog → done/failed)
    ├── TokenTracker          ← per-call token telemetry
    ├── KnowledgeStore        ← structured knowledge base
    ├── SecuritySupervisor    ← approval gating
    └── uvicorn (FastAPI)     ← Web UI + REST API (available in ALL modes)
```

The supervisor is optional. Daemon runs standalone via `python -m claude_works.main`.

---

## Daemon Modes

Daemon runs a state machine with five modes:

| Mode | Trigger | Web UI | Poller | Coordinator |
|------|---------|--------|--------|-------------|
| STARTUP | Process start | ✓ (default port) | ✗ | ✗ |
| INITIALIZE | No valid config/DB | ✓ | ✗ | ✗ |
| MIGRATE | Config/DB exists but wrong schema | ✓ | ✗ | ✗ |
| RUN | All checks pass | ✓ | ✓ | ✓ |
| REPAIR | Runtime error detected | ✓ | ✗ | ✗ |

Web UI starts **first** (before mode detection) — always available.

### Mode Transitions

```
STARTUP → detect_startup_mode()
         → INITIALIZE  (no config file, OR telegram/auth token is placeholder, OR DB init fails + no DB)
         → MIGRATE     (DB init fails + DB exists → schema mismatch)
         → RUN         (all checks pass)

RUN → REPAIR           (runtime error detected)
REPAIR → RUN           (exit_repair() called after fix)

INITIALIZE → RUN       (config becomes valid; _init_poll_loop() polls every 10s)
MIGRATE → *            (MechanicAgent applies migration → re-runs detect_startup_mode())
```

### INITIALIZE mode

Polls `detect_startup_mode()` every 10 seconds. No restart required — when config becomes valid, automatically transitions to RUN and starts all subsystems.

On entry: generates a single-use `setup_token` (32-char hex), logs it to stdout, registers it with the web server via `set_setup_token()`. The Web UI detects INITIALIZE mode on load and shows a setup overlay where the operator enters the token and configures the instance. `POST /api/setup/save` validates fields, writes to the `daemon_config` DB table, and invalidates the token. The next `_init_poll_loop` tick picks up the saved config and auto-transitions to RUN.

### MIGRATE mode

MechanicAgent invoked with `MechanicContext.MIGRATE`. Diagnoses schema/config mismatch, applies migration (ALTER TABLE, missing config keys). After completion, re-runs mode detection.

### REPAIR mode

AgentCoordinator stopped. MechanicAgent invoked with `MechanicContext.REPAIR`. Admin messages (Telegram) routed to `mechanic.followup()`. Exit via `/exit_repair` command or Web UI.

### `claude_works/mode.py`

```python
class DaemonMode(str, Enum):
    STARTUP = "startup"
    INITIALIZE = "initialize"
    MIGRATE = "migrate"
    RUN = "run"
    REPAIR = "repair"

class ModeManager:
    current: DaemonMode
    error: str | None
    since: float  # timestamp
    def transition(self, mode, error=None): ...
    def as_dict(self) -> dict: ...

async def detect_startup_mode() -> tuple[DaemonMode, str | None]:
    # 1. db.init_config() → load daemon_config row → _config_valid() → RUN (config DB takes priority)
    # 2. config.load() (settings.json) — FileNotFoundError → INITIALIZE
    # 3. _config_valid() fails on file config → INITIALIZE
    # 4. db.init() (data DB) + DB exists → MIGRATE (schema mismatch; data DB separate from config DB)
    # 5. no valid config anywhere → INITIALIZE (fresh install)
    # 6. all OK → RUN
```

---

## Module Breakdown

### `claude_works/main.py` — Daemon

Central coordinator. Owns all subsystems, wires them together.

- `start()` — web server starts first, then `detect_startup_mode()` → transition to INITIALIZE/MIGRATE/RUN
- `_init_run_components()` — init DB, TelegramAPI, KanbanBoard, Coordinator, Poller, config watcher → transition to RUN
- `_init_poll_loop()` — polls every 10s in INITIALIZE mode; auto-transitions when config valid
- `_spawn_mechanic(context, mech_mode)` — creates MechanicAgent, starts `_mechanic_loop` task
- `_mechanic_loop()` — runs `mechanic.run_initial()`, stores report, notifies admins
- `trigger_repair(error)` — stops coordinator, spawns mechanic in REPAIR mode
- `exit_repair()` — clears mechanic, calls `_init_run_components()`
- `_on_update()` — dispatch incoming Telegram updates (message / reaction)
- `_handle_message()` — auth check → bundling → `KanbanTask` push → typing indicator
- `_handle_reaction()` — persist reaction, resolve action
- `_handle_command()` — `/auth`, `/block`, `/approve N`, `/deny N`, `/status`, `/reload_persona`, `/reload_config`, `/repair <desc>` (admin), `/exit_repair` (admin)
- `_on_agent_result(task: KanbanTask, result, error)` — security gate → send response → persist bot message
- In REPAIR/MIGRATE mode: admin messages routed to `mechanic.followup(text)`
- `health()` → `{status, poller, active_agents, security_pending, mode, mode_error?, mechanic_report?, rate_limited_until?, llm_usage?}`
- `_usage_poll_loop()` — polls `coordinator.query_usage()` every `llm.usage_poll_interval_seconds` (default 300s); notifies admins once when `usage_pct >= 0.8`; resets notification flag when usage drops below threshold

### `claude_works/llm/` — Provider Abstraction

Provider-agnostic LLM interface. All agents go through this layer — never direct SDK calls.

| File | Responsibility |
|------|---------------|
| `provider.py` | `LLMProvider` ABC, `LLMResponse`, `LLMUsage` dataclasses, `APIProvider`, `CliProvider`, `get_provider(cfg)` factory |
| `errors.py` | `RateLimitError(message, retry_after?)` — typed exception for provider 429s |
| `usage.py` | `UsageStats` dataclass, `parse_usage_text()` — parse `/usage` CLI output |

**Key types:**

```python
@dataclass class LLMUsage:
    input_tokens: int; output_tokens: int
    cache_read_tokens: int = 0; cache_write_tokens: int = 0

@dataclass class LLMResponse:
    text: str; usage: LLMUsage; stop_reason: str = "end_turn"

class LLMProvider(ABC):
    async def complete(self, messages, *, system, model, max_tokens,
                       mcp_servers=None) -> LLMResponse: ...
    async def close(self) -> None: ...
```

`get_provider(cfg)` reads `cfg["provider"]` (default `"api"`) → returns matching `LLMProvider`.
Adding a provider: implement `LLMProvider`, register in `get_provider`.

**Rate limiting:** Both providers raise `RateLimitError(message, retry_after?)` on HTTP 429 / CLI rate limit detection. Callers never see raw SDK exceptions — only `RateLimitError`.

**Usage monitoring** (`CliProvider` only): `query_usage() -> UsageStats | None` sends `/usage` as stdin to the CLI binary. Returns parsed `UsageStats` or `None` if unavailable.

**CliProvider** — uses a CLI binary instead of the direct API. No API key required; uses the subscription associated with the CLI installation.

```python
class CliProvider(LLMProvider):
    async def complete(self, messages, *, system, model, max_tokens, mcp_servers=None):
        # cmd: ["<cli-binary>", "--print", "--output-format", "json", "--model", model, "--system-prompt", full_system]
        # user message via stdin
        # multi-turn: history embedded in system prompt preamble (len(messages) > 1)
        # parses: data["result"], data["usage"]["input_tokens"], etc.
        # cache keys: cache_read_input_tokens, cache_creation_input_tokens
```

`get_provider(cfg)` factory:
- `"api"` → `APIProvider(api_key=cfg["api_key"])`
- `"cli"` → `CliProvider()`

### `claude_works/agents/` — Multi-Layer Agent Architecture

#### `base.py` — BaseAgent ABC

All agents inherit from `BaseAgent`. Handles message history, token tracking, context compaction.

- `__init__(task_id, user_context, agent_class, provider, token_tracker)` — accepts injected provider; `_owns_provider=True` only when provider=None (self-created)
- `_system_prompt() -> str` — abstract; each subclass defines its prompt
- `run(content) -> str` — append message, call `provider.complete()`, log to `token_tracker`, compact at 90% context
- `close()` — closes provider only if `_owns_provider`

#### `coordinator.py` — AgentCoordinator

Replaces `AgentPool`. Shared `LLMProvider` across all agents (single connection pool).

- `start()` — creates shared provider via `get_provider(cfg)`, spawns asyncio tasks: controller loop, chief loop, PO loop, N specialist loops
- `_specialist_loop(agent_class)` — respects per-class `max_parallel`; pauses loop when `_rate_limit_until` in the future (checks every 30s max)
- `_run_specialist(task, agent_class)` — maps class → specialist type, runs with timeout; on success resets `_rate_limit_count`; on `RateLimitError` applies exponential backoff and calls `board.requeue()`; on `BudgetExceededError` calls `board.fail()` + notifies user
- `active_count` property — total running agents
- `is_rate_limited` / `rate_limit_until` properties — cooldown state exposed for `health()`
- `stop()` — cancels all tasks, closes shared provider

**Rate limit cooldown strategy:**
- Hit → `_rate_limit_count++`, cooldown = `min(retry_after * 2^(count-1), 900s)`, task requeued to ASSIGNED
- All specialist loops pause until `_rate_limit_until` passes
- Any successful LLM call resets `_rate_limit_count = 0` (prevents permanent backoff growth)
- Max cooldown: 900s (15 min) regardless of hit count
- REPAIR mode is NOT triggered for rate limits — this is expected operational state, not a failure

Specialist map:
```python
GENERALIST → GeneralistAgent
RESEARCHER → ResearchAgent
CODER      → CodeTeam          # 4-stage pipeline, not a BaseAgent
MEMORY     → MemoryAgent
```

#### `controller.py` — ControllerAgent

Stateless LLM router. Reads BACKLOG, classifies each task, calls `board.assign(task_id, agent_class)`.

- One stateless LLM call per task (`max_tokens=128`), JSON response `{"agent_class": "...", "reason": "..."}`
- Fallback to `GENERALIST` on parse error or unknown class
- Classes: `generalist / researcher / coder / memory / chief / po`
- Routes to `po` for complex multi-step projects requiring decomposition; uses direct specialist for simple tasks

#### `chief.py` — ChiefAgent

Handles CHIEF-class tasks with persona awareness.

- `load_persona()` — reads `PERSONA_FILE` env (default `/data/persona.md`), falls back to `""`
- `reload_persona()` — hot-reload without restart (triggered by `/reload_persona` command)
- `run_loop(on_result)` — picks up tasks assigned to `AgentClass.CHIEF`, runs via `GeneralistAgent` with chief prompt + persona

#### `po.py` — ProductOwnerAgent

Handles `AgentClass.PO` tasks. Decomposes complex goals into subtasks, tracks completion, synthesizes results.

Lifecycle: `ASSIGNED → IN_PROGRESS` (decompose) → `REVIEW` (waiting children) → `DONE/FAILED`

- `_decompose(task)` — LLM call → JSON array of `{title, description, agent_class}`; caps at 8 subtasks; falls back to single generalist task on parse error
- `_synthesize(goal, children)` — formats child results (including failures) → LLM synthesis call
- `_handle_project(task, on_result)` — full lifecycle: start → decompose → `push_child()` for each subtask → review → `await_children()` → synthesize → complete
- `run_loop(on_result)` — picks up `ASSIGNED / PO` tasks, spawns `asyncio.create_task` per project (non-blocking)
- Timeout from `agents.po_timeout_seconds` (default 3600s)

Child tasks are inserted directly into `ASSIGNED` lane via `board.push_child()`, bypassing controller routing.

#### `mechanic.py` — MechanicAgent

Handles `MechanicContext.MIGRATE` (schema/config migration) and `MechanicContext.REPAIR` (runtime error recovery). Invoked by Daemon, not by AgentCoordinator.

```python
class MechanicContext(str, Enum):
    MIGRATE = "migrate"
    REPAIR = "repair"

class MechanicAgent(BaseAgent):
    async def run_initial(self) -> str: ...    # first invocation with context
    async def followup(self, message: str) -> str: ...  # multi-turn continuation
```

System prompt loaded from `agents/mechanic.md` (strips YAML frontmatter). Output format: **Mode** / **Diagnosis** / **Evidence** / **Fix** / **Action** / **Verify**.

#### `specialist/` — Specialist Agents

All extend `BaseAgent`, differ only in `_system_prompt()`:

| Class | File | Addendum |
|-------|------|---------|
| `GeneralistAgent` | `generalist.py` | Base system prompt + optional caveman mode |
| `ResearchAgent` | `researcher.py` | Research-specific instructions |
| `CoderAgent` | `coder.py` | Security standards (OWASP), code quality rules (kept for direct routing) |
| `MemoryAgent` | `memory.py` | Memory retrieval/storage behavior |

#### `specialist/code_team.py` — CodeTeam

Replaces `CoderAgent` in the coordinator's specialist map for `CODER`-class tasks. Runs a sequential 4-stage pipeline using internal `_TeamMember(BaseAgent)` instances sharing the same provider and token_tracker.

Stages:
1. **Architect** — produces technical spec from task content
2. **Developer** — implements based on spec
3. **Tester** — writes tests for spec + implementation
4. **QA** — reviews all outputs, produces final deliverable

All 4 stages log to `token_usage` under `agent_class="coder"`. Constructor signature matches specialist agents (`task_id, user_context, provider, token_tracker, persona`) so coordinator's `_run_specialist()` needs no changes.

### `claude_works/kanban/` — Task Lifecycle

#### `models.py`

```python
class Lane(Enum):
    BACKLOG = "backlog"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"

@dataclass class KanbanTask:
    id, chat_id, user_id, content, lane, agent_class, agent_id,
    parent_id, priority, created_at, assigned_at, started_at,
    completed_at, result, error, message_id
```

#### `board.py` — KanbanBoard

SQLite-backed. `asyncio.Event` wakes waiting loops when tasks enter a lane.

- `push(task)` → insert at `BACKLOG`, notify
- `assign(task_id, agent_class)` → `BACKLOG → ASSIGNED`, notify
- `start(task_id, agent_id)` → `ASSIGNED → IN_PROGRESS`
- `review(task_id)` → move to `REVIEW` (PO waiting for children)
- `complete(task_id, result)` → `IN_PROGRESS → DONE`
- `fail(task_id, error)` → `IN_PROGRESS → FAILED`
- `block(task_id, reason)` → move to `BLOCKED`
- `requeue(task_id)` → `IN_PROGRESS → ASSIGNED` (clears `started_at`/`agent_id`); no-op if not IN_PROGRESS; notifies waiting loops — used on rate limit hit
- `next_backlog()` → oldest BACKLOG task with `parent_id IS NULL` (children bypass controller)
- `next_assigned(agent_class)` → oldest ASSIGNED task for class
- `push_child(task, agent_class)` → insert child directly into `ASSIGNED` lane with `agent_class` set; skips controller routing
- `await_children(parent_id, child_ids)` → polls every 2s until all `child_ids` reach terminal lane (`DONE`/`FAILED`/`BLOCKED`); returns `list[KanbanTask]`
- `wait_for_work(timeout)` → yields until `asyncio.Event` set or timeout

### `claude_works/telemetry/` — Token Tracking + Cost Control

#### `tokens.py` — TokenTracker

Persists every LLM call to `token_usage` table. Calculates cost at insert time via `estimate_cost()`. Enforces spending limits before each API call.

- `log(...)` — insert row including `cost_usd` (calculated from model pricing)
- `get_allowed_model(requested_model)` → model to use or `None` (reject). Checks daily/monthly limits; returns cheaper model on `on_limit_exceeded=downgrade`, `None` on `reject`. Called by `BaseAgent.run()` before every API call.
- `total_cost(since?)` → total USD spent in period
- `stats(since?)` → per-class aggregates including `cost_usd`
- `timeseries(since, bucket_seconds)` → `[{bucket, agent_class, tokens, cost_usd}]`

`BudgetExceededError` raised by `BaseAgent.run()` when `get_allowed_model()` returns `None`.

### `claude_works/knowledge/` — Knowledge Base

#### `store.py` — KnowledgeStore

Structured knowledge entries in SQLite.

- `add(type, title, content, tags, source, user_id)` → insert
- `search(query, user_id=None)` → LIKE search on title + content
- `list_all(user_id=None)` → all entries, optional user filter
- `delete(id)` → remove entry

Schema: `id, type, title, content, tags, source, user_id, created_at, updated_at`

### `claude_works/telegram/`

| File | Responsibility |
|------|---------------|
| `api.py` | httpx-based Telegram Bot API client (send_message, set_reaction, send_chat_action, get_file) |
| `poller.py` | Long-poll loop via `getUpdates`, dispatches to `_on_update` callback |
| `reactions.py` | Emoji → action mapping, extract reaction from update payload |

### `claude_works/tasks/`

| File | Responsibility |
|------|---------------|
| `models.py` | `Task` and `IncomingMessage` dataclasses |
| `queue.py` | Legacy SQLite task queue (kept for compatibility) |
| `bundler.py` | `should_bundle()` + `merge_content()` — time + context heuristic for message merging |

### `claude_works/auth/`

`users.py` — SQLite user table: `upsert_user`, `is_allowed`, `is_admin`, `set_role`

Roles: `admin` (full access) | `user` (allowed) | `blocked` (ignored)

New users land as `blocked`; admin notified via Telegram.

### `claude_works/memory/`

`store.py` — per-user key/value store in SQLite: `set`, `get`, `search`, `list_all`, `delete`

### `claude_works/security/`

Approval gating for critical agent outputs. Disabled by default (`security.enabled: false`).

| File | Responsibility |
|------|---------------|
| `rules.py` | `Rule` dataclass, regex matching, `DEFAULT_RULES`, `build_rules()`, `check_content()` |
| `supervisor.py` | `SecuritySupervisor`: `check()` → triggers on rule match, awaits `asyncio.Event` with timeout; `approve()`/`deny()` |

**Default rules** (all regex, `IGNORECASE|MULTILINE`):

| Type | Pattern | Default |
|------|---------|---------|
| `internet_access` | `https?://\S+` | enabled |
| `data_deletion` | `\b(delete\|drop\|truncate\|wipe\|purge)\b` | enabled |
| `command_execution` | `\b(execute\|subprocess\|shell\|eval)\b` | enabled |
| `external_api` | `\b(webhook\|api_call\|post_to)\b` | disabled |
| `publication` | `\b(publish\|broadcast\|announcement)\b` | disabled |

**Approval flow:**
1. Agent produces result → `Daemon._on_agent_result()` calls `security.check()`
2. Rules match → `PendingApproval` created, admins notified via Telegram
3. Admin approves via `/approve N` (Telegram) or Web UI → `asyncio.Event` set
4. Timeout (default 300s) → auto-deny
5. Audit trail in `security_approvals` SQLite table

### `claude_works/web/`

FastAPI app served by uvicorn (same process as Daemon, separate asyncio task).

**Auth:** `X-Auth-Token` header or `auth` cookie — SHA256 of `web.auth_token` from settings.

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Public health check |
| GET | `/api/setup` | Public — returns `{mode, setup_required}` for INITIALIZE detection |
| POST | `/api/setup/save` | Token-gated (no auth cookie) — save initial config to DB; single-use token |
| GET | `/api/status` | Auth'd status (poller, active_agents, security_pending) |
| GET | `/api/tasks` | Legacy task list (filter by status, limit) |
| GET | `/api/messages` | Incoming messages (filter by chat_id, limit) |
| GET | `/api/users` | User list |
| POST | `/api/users/{id}/role` | Update user role |
| GET | `/api/memory` | Memory entries (filter by user_id, search query) |
| GET | `/api/approvals` | Pending security approvals |
| POST | `/api/approvals/{id}/approve` | Approve pending action |
| POST | `/api/approvals/{id}/deny` | Deny pending action |
| GET | `/api/kanban` | Kanban tasks (filter by `lane`, `limit`) |
| GET | `/api/kanban/counts` | `{lane: count}` summary for all lanes |
| GET | `/api/tokens` | Token usage stats + timeseries (`period`: `1h`/`24h`/`7d`/`30d`) |
| POST | `/api/config/reload` | Reload settings.json from disk immediately |
| GET | `/api/logs` | Last N lines of log file |
| GET | `/api/mode` | Current daemon mode and error |
| POST | `/api/repair/trigger` | Trigger REPAIR mode with error description |
| POST | `/api/repair/exit` | Exit REPAIR mode, resume RUN |
| POST | `/api/repair/chat` | Send message to active MechanicAgent |
| GET | `/api/repair/report` | Latest mechanic diagnosis report |
| GET | `/api/usage` | LLM CLI usage stats (`cli` provider only; null fields if unavailable) |
| GET | `/` | Web UI (index.html) |

**Token API response shape:**
```json
{
  "period": "24h",
  "total_cost_usd": 0.042,
  "stats": {
    "generalist": {"input": 1200, "output": 340, "cache_read": 800, "cache_write": 0, "cost_usd": 0.042, "calls": 5}
  },
  "timeseries": [{"bucket": 1718100000, "agent_class": "generalist", "tokens": 450, "cost_usd": 0.012}]
}
```
Bucket size: 3600s for ≤24h, 21600s for >24h.

**Web UI tabs:** Tasks · Messages · Users · Memory · Approvals · Kanban · Tokens · Logs

### `claude_works/config_store.py` — DB Config Store

Three async helpers for the `daemon_config` table:

- `save_config(conn, cfg)` — INSERT OR REPLACE row id=1 with JSON-serialized config
- `load_config(conn)` → `dict | None` — fetch and deserialize; `None` if no row
- `delete_config(conn)` — remove config row (used by MechanicAgent during migrations)

### `claude_works/logging_setup.py`

`setup()` — configures root logger: `RotatingFileHandler` (10 MB × 5 backups) + `StreamHandler`. Integrates uvicorn log config.

`log_path(dir)` → `Path` to current log file.

### `supervisor/supervisor.py`

External watchdog process. Polls `/health` endpoint every N seconds. On failure: restart Daemon with exponential backoff. After `max_restart_attempts` failures: Telegram alert to admins.

---

## Data Storage

All user-owned state lives under `/data`. The container image is immutable; nothing user-specific is stored inside it.

```
/data/
├── settings.json          # main config
├── config.db              # daemon config DB (CONFIG_DB_FILE override)
├── claude-works.db               # operational DB (DB_FILE override)
├── persona.md            # optional ChiefAgent persona
├── logs/
│   ├── claude-works.log          # rotating application log
│   └── init.log           # container startup log (requirements.local.txt, init.sh output)
├── requirements.local.txt # optional: extra pip packages installed at each startup
└── init.sh                # optional: custom shell commands run at each startup
```

`requirements.local.txt` and `init.sh` are the canonical way to extend the container without rebuilding the image. Both are executed by `entrypoint.sh` on every start; output is appended to `init.log` with timestamps.

### SQLite databases

Two SQLite databases, both in WAL mode with `synchronous=NORMAL`.

**`/data/config.db`** — daemon configuration (path override: `CONFIG_DB_FILE` env var)

| Table | Purpose |
|-------|---------|
| `daemon_config` | Single-row config store (id=1, settings_json TEXT, updated_at); takes priority over settings.json on startup |

**`/data/claude-works.db`** — operational data (path override: `DB_FILE` env var)

| Table | Purpose |
|-------|---------|
| `tasks` | Legacy task queue (status, content, result) |
| `messages` | Incoming Telegram messages |
| `bot_messages` | Outgoing bot messages (telegram_message_id → task_id) |
| `reactions` | Telegram reactions with resolved action |
| `users` | User profiles and roles |
| `memory` | Per-user key/value memory store |
| `kanban_tasks` | Kanban task lifecycle (lane, agent_class, parent_id, timestamps) |
| `token_usage` | Per-call token telemetry (agent_class, model, input/output/cache tokens, cost_usd) |
| `knowledge` | Structured knowledge base (type, title, content, tags, source) |
| `agent_sessions` | Agent session lifecycle |
| `security_approvals` | Security approval audit trail |

**Indexes:** `kanban_tasks(lane)`, `kanban_tasks(lane, agent_class)`, `kanban_tasks(user_id, lane)`, `token_usage(timestamp)`, `token_usage(agent_class, timestamp)`, `knowledge(type)`, `knowledge(user_id, updated_at)`

---

## Configuration (`settings.json`)

```json
{
  "telegram":  { "token", "allowed_updates", "admin_chat_ids", "reaction_map" },
  "llm":       {
    "provider",
    "api_key", "model", "max_tokens",
    "max_context_tokens", "context_compact_threshold"
  },
  "agents":    {
    "max_parallel", "task_timeout_seconds", "po_timeout_seconds", "max_retries",
    "model_tiers": {"fast": "<model-id>", "balanced": "<model-id>", "best": "<model-id>"},
    "models": {
      "default":    "<tier-or-model-id>",
      "controller": "<tier-or-model-id>",
      "chief":      "<tier-or-model-id>",
      "po":         "<tier-or-model-id>",
      "generalist": "<tier-or-model-id>",
      "researcher": "<tier-or-model-id>",
      "memory":     "<tier-or-model-id>",
      "compactor":  "<tier-or-model-id>",
      "coder": {
        "default":   "<tier-or-model-id>",
        "architect": "<tier-or-model-id>",
        "developer": "<tier-or-model-id>",
        "tester":    "<tier-or-model-id>",
        "qa":        "<tier-or-model-id>"
      },
      "mechanic":   "<tier-or-model-id>"
    }
  },
  "web":       { "port", "host", "auth_token" },
  "users":     { "default_role", "admin_ids" },
  "supervisor":{ "health_check_interval", "max_restart_attempts", "restart_backoff_seconds" },
  "security":  { "enabled", "pending_timeout_seconds", "rules" },
  "mcp":       { "enabled", "servers" },
  "spending":  {
    "max_daily_usd",
    "max_monthly_usd",
    "on_limit_exceeded",
    "model_pricing": {"<model-id>": {"input_per_mtok": ..., "output_per_mtok": ...}}
  },
  "logging":   { "dir", "level" }
}
```

`llm.provider`: `"api"` (default). Values: `"api"` | `"cli"`.

**Per-agent model selection** (`agents.models` + `agents.model_tiers`): each agent class and CodeTeam stage resolves its model via `get_agent_model(agent_class, stage?)` in `config.py`.

Lookup order:
1. `agents.models.<agent_class>[.<stage>]` — explicit override in settings.json
2. `_AGENT_CLASS_TIERS` / `_CODER_STAGE_TIERS` — per-class tier assignment in code
3. `agents.model_tiers.<tier>` — tier → model ID mapping in settings.json
4. `_TIER_DEFAULTS` — hardcoded tier fallbacks (fast/balanced/best)

**Tier aliases** (`fast` / `balanced` / `best`): model values at any level can be tier names or direct model IDs. Update `agents.model_tiers.best` in settings.json when a new top-tier model releases — all `best`-assigned agents upgrade automatically without code changes.

```json
"model_tiers": {"fast": "...", "balanced": "...", "best": "<new-model-id>"},
"models": {"chief": "best", "controller": "fast", "coder": {"tester": "fast"}}
```

`coder` supports nested dict for stage-level overrides (architect/developer/tester/qa). All other classes take a string (tier alias or direct ID). No restart required.

See `settings.example.json` for full structure with all fields and placeholders.

**Hot-reload:** Settings are watched automatically (mtime check every 5s). Any change to `settings.json` takes effect immediately — no restart required. Changes to agent models, spending limits, reaction map, security rules, etc. apply to the next API call after reload. Only startup-wired values (Telegram token, DB path, web port) require restart.

Manual reload triggers:
- Telegram command `/reload_config` (admin only)
- `POST /api/config/reload` (Web API)

---

## Message Flow

```
Telegram → TelegramPoller._poll()
         → Daemon._on_update()
         → _handle_message()
             → upsert_user / auth check
             → react 👀 (receipt)
             → bundling window (2s)
             → KanbanBoard.push(KanbanTask)    ← BACKLOG lane
             → _start_typing()

ControllerAgent.run_loop()
    → KanbanBoard.next_backlog()               ← awaits event
    → LLMProvider.complete() [stateless route]
    → KanbanBoard.assign(task_id, agent_class) ← ASSIGNED lane

AgentCoordinator._specialist_loop(class)
    → KanbanBoard.next_assigned(class)         ← awaits event
    → agent.run(content)                        ← IN_PROGRESS lane
        → LLMProvider.complete()
        → TokenTracker.log()
    → KanbanBoard.complete() or .fail()        ← DONE/FAILED lane
    → _on_agent_result()
        → SecuritySupervisor.check()           ← optional gate
        → TelegramAPI.send_message()
        → persist bot_messages

ProductOwnerAgent.run_loop()                   ← parallel to specialist loops
    → KanbanBoard.next_assigned(PO)
    → asyncio.create_task(_handle_project)
        → KanbanBoard.start()                  ← IN_PROGRESS
        → _decompose(task)                     ← LLM → JSON subtask list
        → KanbanBoard.push_child() × N         ← children → ASSIGNED (skip controller)
        → KanbanBoard.review()                 ← parent → REVIEW
        → KanbanBoard.await_children()         ← polls until all terminal
        → _synthesize(goal, children)          ← LLM → final result
        → KanbanBoard.complete()               ← DONE
        → _on_agent_result()
```