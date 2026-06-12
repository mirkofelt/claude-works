# Developer Mode

Developer mode unlocks live update and deployment features. It is off by default to prevent accidental changes in production.

## Enabling

Settings → Developer Mode → toggle on. Saved immediately to daemon config under `system.dev_mode`.

When on:
- Deploy Guard configuration section becomes visible
- `/api/deploy/trigger` and `/api/deploy/rollback` endpoints become active
- Deploy buttons appear in the Settings UI

When off:
- All deploy endpoints return `403`
- Deploy Guard config fields are hidden

---

## Deploy Guard

A minimal webhook container that controls the Docker lifecycle of `claude-works`. It is the only component allowed to touch the Docker socket for this service.

### What it does

| Endpoint | Action |
|---|---|
| `GET /health?token=…` | Liveness check — returns `{"status":"ok"}` |
| `POST /deploy?token=…` | Pull latest image, recreate container, health check, auto-rollback on failure |
| `POST /rollback?token=…` | Restart container with previous image tag |

### Security constraints

- Token required on every request (compared with `hmac.compare_digest`)
- Only touches the hardcoded service name (`claude-works`) — no user-controlled shell input
- Rate-limited: max 1 deploy per 5 minutes
- Binds to `127.0.0.1:9876` only — not reachable from outside the host without explicit tunnel

### Setup

**1. Generate a token**

```bash
openssl rand -hex 32
```

**2. Add to `.env` on the Unraid host**

```
DEPLOY_TOKEN=<your-token>
```

**3. Start claude-guard**

```bash
docker-compose up -d claude-guard
```

The `claude-guard` service is defined in `docker-compose.yml`. It mounts:
- `/var/run/docker.sock` — to control Docker
- `./docker-compose.yml:/compose/docker-compose.yml:ro` — to recreate the service

**4. Configure in Web UI**

Settings → Developer Mode → Deploy Guard:
- Guard URL: `http://claude-guard:9876` (Docker internal network)
- Deploy Token: paste the token from Step 1
- Click **Test Connection** to verify

---

## CI Auto-Deploy (optional)

After a successful build on `main`, GitHub Actions can trigger a deploy automatically.

Add two secrets to the GitHub repository:
- `DEPLOY_WEBHOOK_URL` — the claude-guard URL reachable from CI (e.g. via Cloudflare Tunnel or home VPN)
- `DEPLOY_TOKEN` — the same token as above

The CI workflow will POST to `/deploy` after a successful Docker build. If the variable is not set, the step is skipped silently.

---

## Rollback

Deploy guard saves the current image tag before every deploy. If the new container fails its health check within 30 seconds, it automatically restores the previous tag.

Manual rollback via Web UI: Settings → Developer Mode → **Rollback** button (visible when dev mode is on and guard is connected).

Manual rollback via CLI:

```bash
curl -sf -X POST "http://localhost:9876/rollback?token=YOUR_TOKEN"
```

---

## Branch Strategy

- `dev` — active development; CI builds `:dev` tag; no auto-deploy
- `main` — production-ready; CI builds `:latest` and `:v{sha}` tags; optional auto-deploy via claude-guard
