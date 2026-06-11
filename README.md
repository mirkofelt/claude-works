# Claude Works

Non-blocking, multi-agent Telegram communication system. Standalone Docker container — one volume, zero config files in the image.

## Installation

### 1. Pull image

```bash
docker pull ghcr.io/mirkofelt/claude-works:latest
```

Or build locally:

```bash
git clone https://github.com/mirkofelt/claude-works
cd claude-works
docker compose up -d
```

### 2. Mount `/data`

The only required mount. All user state lives here — config, knowledge, prompts, logs.

```yaml
# docker-compose.yml (minimal)
services:
  claude-works:
    image: ghcr.io/mirkofelt/claude-works:latest
    volumes:
      - ./data:/data
    ports:
      - "8080:8080"
```

### 3. First start — two options

**Option A: Web wizard (interactive)**

Start the container. Open `http://localhost:8080`, enter the one-time setup token printed to stdout, fill in the form. Done.

**Option B: settings.json (headless / scripted)**

Place a `settings.json` in `/data/` before starting. The daemon reads it automatically, skips the wizard, and goes straight to RUN mode.

```bash
cp settings.example.json data/settings.json
# edit data/settings.json — fill in YOUR_* placeholders
docker compose up -d
```

Required fields in `settings.json`:

```json
{
  "telegram": { "token": "BOT_TOKEN", "admin_chat_ids": [NUMERIC_USER_ID] },
  "llm": { "provider": "api", "api_key": "YOUR_API_KEY" },
  "web": { "auth_token": "YOUR_SECRET" }
}
```

All other fields are optional — see `settings.example.json` for the full structure.

### 4. Claude CLI (optional, for subscription users)

The `claude` binary is bundled in the container. Set `llm.provider = "cli"` in the Settings tab, then authenticate:

**Option A — Telegram** (recommended): Send `/reauth` to the bot. It sends an auth URL; open it in your browser, then send the code back via Telegram.

**Option B — Web UI**: Settings → AI Provider → `cli` → "Authenticate with Anthropic →". Opens an auth flow inside the web UI.

**Option C — Shell**:
```bash
docker compose exec claude-works claude auth login
```

Auth credentials persist in `/data/.claude/` across container restarts.

---

## Import from AI Agent

Any AI assistant with persistent memory (ClaudeClaw, ChatGPT, etc.) can export its knowledge into claude-works for use by agents.

### How it works

Files placed in `/data/knowledge/` are automatically imported into the knowledge base on every container start. Changed files are re-imported. Deleted files leave the KB entry intact.

### Copyable prompt for agents

Paste this prompt to any AI assistant that knows you:

```
Create a claude-works knowledge export from your current context and memory.
Generate the following files:

knowledge/01_persona.md
  — Your character: name, communication style, behavioral rules, how you present yourself.

knowledge/02_user_profile.md
  — User profile: person, job, family, contacts, tech stack, communication preferences.

knowledge/03_projects.md
  — Active projects: descriptions, status, priorities, technical details, architecture.

knowledge/04_properties.md (if known)
  — Real estate: construction details, costs, open invoices, contractors.

knowledge/05_travel.md (if known)
  — Planned trips: booking status, dates, deadlines, itineraries.

knowledge/06_rules.md
  — Behavioral feedback: what to do, what to avoid, trust levels, system quirks.

knowledge/07_*.md
  — Any additional topics from memory (business ideas, health, subscriptions, etc.)

Format rules:
- Markdown only, no YAML frontmatter needed
- Tables preferred over prose for structured data
- Include dates where relevant ("as of June 2026")
- Highlight deadlines and open tasks explicitly
- No credentials, passwords, or API keys in any file

Also create settings.json using this template:
[paste contents of settings.example.json here]
Replace all YOUR_* placeholders — leave them as-is if unknown.
```

### File layout

```
/data/knowledge/
  01_persona.md
  02_user_profile.md
  03_projects.md
  04_properties.md
  05_travel.md
  06_rules.md
  07_*.md         ← any additional topics
```

---

## /data layout

```
/data/
  config.db              # daemon config (managed via Web UI or settings.json seed)
  claude-works.db        # operational DB (auto-created)
  settings.json          # optional seed: read once on first start if no config.db
  .claude/               # Claude CLI auth (CLAUDE_HOME)
  prompts/               # agent prompt overrides (auto-exported from image on first start)
  knowledge/             # knowledge documents (auto-imported on every start)
  projects/              # working directory for Claude CLI subprocess
  logs/
    claude-works.log     # rotating application log
    tor.log              # Tor daemon log
    init.log             # container startup log
  requirements.local.txt # optional: extra pip packages, installed at startup
  init.sh                # optional: custom shell commands run at startup
```

---

## Features

- **Multi-agent** — specialist pool (generalist, researcher, coder, memory) + ChiefAgent + ProductOwner for complex tasks
- **Output patterns** — `[VOICE:]` TTS, `[MAP:]` location, `[BUTTONS:]` inline keyboard, `[SEND_EMAIL:]`, `[READ_EMAIL:]`, `[GITHUB_API:]`
- **Security Officer** — LLM reviews all outbound content for data leaks before sending
- **Tor routing** — URL fetches via Tor by default; asks user if blocked
- **Hot-reload** — edit `/data/prompts/*.md`, active within 5 seconds; no restart needed
- **Knowledge base** — auto-import from `/data/knowledge/` on start; FTS5 search
- **Web UI** — dark dashboard, all config editable live (Settings tab)

---

## Development

```bash
pip install -r requirements-dev.txt
pytest
python -m claude_works.main
```

Branch strategy: `feature/*` → PR to `develop` → release merge to `main`.
