# Claude Works

Claude Works — non-blocking, multi-agent Telegram communication system. Standalone Docker container.

## Features

- **Multi-layer agent architecture** — ControllerAgent routes tasks by type; specialist pool (generalist / researcher / coder / memory) + ChiefAgent for strategy
- **Provider-agnostic LLM layer** — all API calls via `LLMProvider` ABC; swap backends by changing one config key
- **Kanban task lifecycle** — tasks flow through backlog → assigned → in_progress → review → done/failed; asyncio event-driven, no polling
- **Token telemetry** — per-call tracking by agent class + model; time-series aggregation; visible in Web UI
- **Knowledge base** — structured knowledge store (type/title/content/tags/source), agent-accessible
- **Non-blocking message processing** — Telegram poller never blocks; all work runs in background agents
- **Message bundling** — rapid follow-ups merged into single task via time + context heuristic
- **Reaction handling** — Telegram reactions mapped to configurable actions
- **User auth** — role-based (admin/user/blocked), new users auto-blocked pending admin approval
- **Per-user memory** — SQLite key/value store, agent-scoped per user
- **MCP support** — configurable MCP servers passed to LLM provider via beta API
- **Security supervisor** — gating layer for critical operations (internet access, data deletion, etc.); approve via Telegram or Web UI
- **Web UI** — dark dashboard at port 8080; tabs: Tasks, Messages, Users, Memory, Approvals, Kanban, Tokens, Logs
- **Setup wizard** — INITIALIZE mode: Web UI setup overlay with single-use token; no API calls during setup
- **Supervisor process** — health-check loop, auto-restart with backoff, Telegram alert on failure
- **Structured logging** — rotating log file, uvicorn integrated
- **Split DB** — config (`config.db`) separate from operational data (`claude-works.db`)

## Quick Start

```bash
docker compose up -d
```

On first start the daemon enters **INITIALIZE** mode. Open `http://localhost:8080`, enter the one-time setup token printed to stdout, and fill in the configuration form. All settings are stored in `config.db` — no `settings.json` needed.

## Configuration

All config is stored in `config.db` (the `daemon_config` table). The Web UI setup wizard is the primary way to configure the system. See `settings.example.json` for the full config structure and available keys.

Key sections: `telegram`, `llm`, `agents`, `web`, `users`, `supervisor`, `security`, `mcp`, `logging`

Config hot-reload: the daemon polls `config.db` every 5 seconds. Changes saved via the Web UI take effect without a restart. For an immediate reload: `/reload_config` (Telegram) or `POST /api/config/reload` (Web API).

Environment variables:
- `DB_FILE` — path to operational DB (default: `/data/claude-works.db`)
- `CONFIG_DB_FILE` — path to config DB (default: `/data/config.db`)

## /data layout

Everything user-owned lives under `/data`. The container image is read-only; no user state is stored inside it.

```
/data/
├── settings.json          # main config (required)
├── config.db              # daemon config DB (auto-created)
├── claude-works.db               # operational DB (auto-created)
├── persona.txt            # optional: ChiefAgent persona
├── logs/
│   ├── claude-works.log          # rotating application log
│   └── init.log           # container startup log (see below)
├── requirements.local.txt # optional: extra pip packages, installed at each startup
└── init.sh                # optional: custom shell commands run at each startup
```

### Extending the container

Rather than modifying the image, place customisations in `/data` — they survive container rebuilds and are self-documenting:

| File | Purpose |
|------|---------|
| `requirements.local.txt` | Extra Python packages (`pip install -r`) |
| `init.sh` | Arbitrary shell commands (apt installs, tool downloads, env setup) |

Both are executed on every container start before the application launches. Output is appended to `/data/logs/init.log` with timestamps, so the history of all container modifications is always available in `/data`.

## Architecture

See `docs/architecture.md` for full module breakdown and data flow.

## Development

```bash
pip install -r requirements.txt
pytest
python -m claude_works.main
```
