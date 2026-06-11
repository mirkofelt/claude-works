# MCP Server Integration

claude-works supports MCP (Model Context Protocol) servers running as stdio subprocesses. Agents get MCP tools automatically on every invocation when MCP is enabled. No bind mounts, no image rebuilds — everything runs from the `/data/plugins/` volume.

## Architecture

```
Agent → CliProvider.complete()
           ↓  writes temp /tmp/mcp_xxx.json
        claude --print --mcp-config /tmp/mcp_xxx.json
           ↓  spawns stdio subprocesses
        uv run python /data/plugins/<name>/server.py
```

Config lives in `config.db → settings_json`:
- `mcp.enabled` (bool) — master switch
- `mcp.servers` (list) — server definitions: name, command, args, env

## Setup (agent-driven, no manual steps)

Ask the agent to set up loxone/zehnder MCP and it will walk through this automatically:

### Step 1 — Clone server repos

```
GIT_CLONE: https://github.com/mirkofelt/loxone-mcp | loxone-mcp
GIT_CLONE: https://github.com/mirkofelt/zehnder-mcp | zehnder-mcp
```

Code lands in `/data/plugins/loxone-mcp/` and `/data/plugins/zehnder-mcp/` — both inside the already-mounted `/data` volume.

### Step 2 — Provide credentials

Either fill in **Settings → Plugin Config** in the web UI for `loxone` and `zehnder`, or tell the agent the values directly. The agent stores them via `PLUGIN_CONFIG_SET`.

Loxone fields: `host`, `user`, `password`
Zehnder fields: `host`, `gateway_uuid`, `client_uuid`

### Step 3 — Enable and register

The agent does this automatically once credentials are available:

```
CONFIG_UPDATE: mcp.enabled | true
CONFIG_UPDATE: mcp.servers | [
  {
    "name": "loxone",
    "command": "uv",
    "args": ["run", "--project", "/data/plugins/loxone-mcp", "python", "/data/plugins/loxone-mcp/server.py"],
    "env": {"LOXONE_HOST": "...", "LOXONE_USER": "...", "LOXONE_PASSWORD": "..."}
  },
  {
    "name": "zehnder",
    "command": "uv",
    "args": ["run", "--project", "/data/plugins/zehnder-mcp", "python", "/data/plugins/zehnder-mcp/server.py"],
    "env": {"ZEHNDER_HOST": "...", "ZEHNDER_GATEWAY_UUID": "...", "ZEHNDER_CLIENT_UUID": "..."}
  }
]
```

No restart needed. Takes effect on the next agent call.

## Adding other MCP servers

Same pattern works for any stdio MCP server:

```
GIT_CLONE: https://github.com/owner/my-mcp-server | my-server
CONFIG_UPDATE: mcp.servers | [<existing servers...>, {"name":"my-server","command":"uv","args":[...],"env":{...}}]
```

## Disabling MCP

```
CONFIG_UPDATE: mcp.enabled | false
```

## Notes

- `uv` is included in the container image and manages per-project virtualenvs automatically.
- First run downloads dependencies into `/data/plugins/<name>/.venv` — takes ~10–30s depending on network.
- MCP server processes are spawned fresh per agent invocation (no persistent connection).
- Only works with CLI provider (`llm.provider = "cli"`). API provider uses a separate path.
- Credentials are stored in `config.db` (inside the `/data` volume, not in any repo).
