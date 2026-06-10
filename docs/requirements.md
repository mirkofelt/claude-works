# Comms — Requirements

## Purpose

Comms is a personal AI assistant running as a self-hosted Docker container. It receives messages via Telegram, processes them through a multi-layer agent architecture, and responds. It is designed for autonomous operation with low operational overhead and predictable costs.

---

## Functional Requirements

### F1 — Communication
- Natural language input, no command prefix required
- Telegram reactions: receive and evaluate (message_reaction)
- Voice messages as input (STT → Text)
- Bot can set reactions on outgoing messages

### F2 — Non-blocking Message Processing
- Incoming message → immediate ACK (Telegram "typing..." indicator)
- Processing in background task, Telegram listener never blocked
- New message during active processing:
  - Option A: cancel existing task, start new
  - Option B: queue new task, process sequentially
  - Option C: merge new message context into running task
  - Decision via heuristic (short follow-up → C; fresh question → A)

### F3 — Task Management
- Each message → task with unique ID
- Task states: `pending → in_progress → done | failed | cancelled`
- Tasks are persistent (SQLite), survive restarts
- Task fields: message_id, chat_id, content, created_at, status, assigned_agent, result

### F4 — Agent Pool
- Configurable max parallel agents (default 4)
- Each agent: own LLM context
- Agent lifecycle: spawn → work → teardown (no leaks)
- Agents share resources (settings, tools) but not context

### F5 — Agent Concepts (required for all agents)
- TDD: tests before code
- Privacy: no personal data in logs, commits, outputs
- No hanging processes: timeout (default 10 min), then kill + retry
- Context Budget: warn at 80% utilization, compact at 90%

### F6 — Context Overflow Prevention
- Each agent tracks own token count estimate
- At 80%: automatic summary + handoff to fresh agent
- Never lose task through context overflow

### F7 — Data Storage
- All incoming messages stored (message_id, from, text, timestamp)
- Outgoing messages stored (bot message_id for reaction context)
- Reactions stored with reference to original message
- Two SQLite databases: `config.db` (daemon config) and `claude-works.db` (operational data)

### F8 — Process Hygiene
- No hanging process longer than defined timeout
- SIGTERM handler: drain queue, graceful agent shutdown
- Watchdog: detect dead agents, auto-respawn
- Health endpoint: GET /health → JSON with agent status, queue depth

---

## Non-Functional Requirements

### NF1 — Stack
- Python, asyncio, no blocking I/O in main loop
- SQLite for persistence
- Settings from `settings.json` (no hardcoding)

### NF2 — Deployment
- Runs as Docker container or standalone process
- Config via `settings.json`; env var `SETTINGS_FILE` overrides path
- Setup wizard in Web UI for first-run configuration (INITIALIZE mode)

### NF3 — Security
- No credentials in logs or code
- All external calls via HTTPS
- Only whitelisted chat IDs processed
- Rate-limiting: max N messages/minute

### NF4 — Observability
- Structured logging with timestamps
- Every task transition logged
- Agent spawn and teardown logged
- Rotating log file

---

## LLM Provider

Two provider types supported via `llm.provider` config key:

- `"api"` — direct API calls (requires `llm.api_key`)
- `"cli"` — local CLI binary in PATH (uses subscription, no API key)

Provider and api_key are read at startup only. Changing them requires a daemon restart.

---

## Additional Design Notes

**Multi-message Bundling**: Two messages belong together when time gap < 5s AND context is related (follow-up, correction) OR first message ends with open structure. Pure time threshold is insufficient.

**Reaction as First-Class Feedback**: Reactions are full interactions.
- 👍 = approve / done
- 👎 = wrong, retry
- ❤️ = save / mark important
- 🔥 = urgent / high priority
- 😂 = dismiss / was a joke
- 🤔 = unclear, clarify
- Custom mapping configurable in settings

**Typing Indicator Loop**: Send "typing..." every 4s while agent works.

**Voice Output**: Optional ElevenLabs integration for voice responses.

**Priority Queue**: Messages with "!" prefix → high priority, overtakes running tasks.

**Agent Memory**: Shared SQLite memory store all agents can read/write.

---

## Web UI

### W1 — Dashboard
- Real-time overview: running agents, task queue, system status
- Live updates via WebSocket

### W2 — Knowledge Base
- Browseable memory store by tag, user, topic
- Full-text search
- Edit, delete, add entries

### W3 — Configuration
- Settings editable via UI
- User management: approve, set roles, block
- Reaction mapping configurable

### W4 — Process Monitoring
- Log viewer (filterable by level/agent/user)
- Task history with timeline

### W5 — Auth
- Token-based login
- Session-based, no plaintext password

---

## Heartbeat & Self-Healing

- Internal heartbeat every 60s
- Telegram polling crashed → auto-restart (max 3 attempts, then alert)
- Agent timeout → SIGTERM → SIGKILL → requeue
- All errors → Telegram notification to admin

---

## Multi-User & Auth

- Multiple users, each with own profile in SQLite
- Roles: `admin`, `user`, `blocked`
- New users are `blocked` by default
- Only whitelisted user IDs processed
- Tasks are user-scoped

---

## Out of Scope (initially)
- Group chats
