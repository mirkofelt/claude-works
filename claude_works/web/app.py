import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .deps import verify_token as _verify_token  # re-export: tests override this via app.dependency_overrides
from .routes import admin, auth, config as config_routes, kanban, knowledge, tokens, users
from .state import set_daemon, set_setup_token

app = FastAPI(title="Claude Works", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],  # no cross-origin access; UI is served same-origin
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-Auth-Token", "Content-Type"],
)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(kanban.router)
app.include_router(knowledge.router)
app.include_router(users.router)
app.include_router(config_routes.router)
app.include_router(tokens.router)


@app.get("/health")
async def health():
    from . import state
    if state.daemon_ref:
        return state.daemon_ref.health()
    return {"status": "ok", "mode": "startup"}


@app.get("/", response_class=HTMLResponse)
async def index():
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        with open(index_path) as f:
            return f.read()
    return "<h1>Claude Works</h1><p>UI not built yet.</p>"
