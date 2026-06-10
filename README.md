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
cp settings.example.json /data/settings.json
# fill in telegram.token, llm.api_key, web.auth_token, users.admin_ids
docker compose up -d
```

Web UI: `http://localhost:8080` — auth token from `settings.json → web.auth_token`

Alternatively, start without a config file. The daemon enters INITIALIZE mode and shows a setup wizard in the Web UI. Check server logs for the one-time setup token.

## Configuration

All config lives in `/data/settings.json` (override path via `SETTINGS_FILE` env var). See `settings.example.json` for all options.

Key sections: `telegram`, `llm`, `agents`, `web`, `users`, `supervisor`, `security`, `mcp`, `logging`

Environment variables:
- `SETTINGS_FILE` — path to settings.json (default: `/data/settings.json`)
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
