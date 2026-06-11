# MCP Server Integration

claude-works supports MCP (Model Context Protocol) servers running as stdio subprocesses inside the container. Agents get MCP tools automatically on every invocation when MCP is enabled.

## Architecture

```
Agent → CliProvider.complete()
           ↓  writes temp /tmp/mcp_xxx.json
        claude --print --mcp-config /tmp/mcp_xxx.json
           ↓  spawns stdio processes
        uv run python /data/plugins/<name>/server.py
```

The config lives in `config.db → settings_json.mcp`:
- `mcp.enabled` (bool) — master switch
- `mcp.servers` (list) — server definitions with name, command, args, env

## Setup

### 1. Mount plugin directories

Use `docker-compose.plugins.yml` alongside the main compose file:

```bash
# .env
LOXONE_MCP_DIR=/path/to/loxone-mcp
ZEHNDER_MCP_DIR=/path/to/zehnder-mcp
```

```bash
docker-compose -f docker-compose.yml -f docker-compose.plugins.yml up -d
```

Or add bind mounts manually in a `docker-compose.override.yml`.

Plugin directories are mounted read-only at `/data/plugins/<name>/` inside the container.

### 2. Configure via agent or CONFIG_UPDATE tag

Enable MCP and register servers in config.db:

```
CONFIG_UPDATE: mcp.enabled | true
CONFIG_UPDATE: mcp.servers | [
  {
    "name": "loxone",
    "command": "uv",
    "args": ["run", "--project", "/data/plugins/loxone-mcp", "python", "/data/plugins/loxone-mcp/server.py"],
    "env": {
      "LOXONE_HOST": "192.168.x.x",
      "LOXONE_USER": "admin",
      "LOXONE_PASSWORD": "secret"
    }
  },
  {
    "name": "zehnder",
    "command": "uv",
    "args": ["run", "--project", "/data/plugins/zehnder-mcp", "python", "/data/plugins/zehnder-mcp/server.py"],
    "env": {
      "ZEHNDER_HOST": "192.168.x.x",
      "ZEHNDER_GATEWAY_UUID": "1fff7a107a1040008000144fd7100000",
      "ZEHNDER_CLIENT_UUID": "c1a4c0de000000000000000000000001"
    }
  }
]
```

Config takes effect on the next agent invocation — no restart needed.

### 3. Install new MCP servers via agent

Agents can clone and configure new MCP servers themselves:

```
GIT_CLONE: https://github.com/owner/mcp-server | server-name
```

Then instruct the agent to configure it via CONFIG_UPDATE.

## Credential storage

Credentials are stored in the `env` field of each server entry in `config.db`. This is encrypted-at-rest by filesystem permissions on `/data/`. Do not commit actual credentials — use CONFIG_UPDATE or the Web UI Settings panel.

## Disabling MCP

```
CONFIG_UPDATE: mcp.enabled | false
```

## Constraints

- Only works with CLI provider (`llm.provider = "cli"`). API provider uses a different MCP path (Anthropic beta, HTTP-only).
- `uv` must be available in the container (included in the image since commit `b9cc717`).
- Plugin dirs must be mounted into the container — the server code is not bundled in the image.
- Each agent invocation spawns fresh MCP server processes (no persistent connection).
