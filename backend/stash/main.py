"""FastAPI application: REST API under /api, the built web UI everywhere else."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, jobs, processing  # noqa: F401  (registers job handlers)
from .api import assets, integrations, library
from .api.common import SESSION_COOKIE, auth_enabled, is_authorized, session_token
from .config import settings
from .db import init_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("stash")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_engine()
    log.info("Stash %s - data: %s - blender: %s", __version__, settings.data_dir, settings.blender or "not available")
    pool = jobs.start_workers() if settings.workers > 0 else None
    yield
    if pool:
        pool.shutdown()


app = FastAPI(title="Stash", version=__version__, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

PUBLIC_API = ("/api/auth/", "/api/status")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith(PUBLIC_API) and request.method != "OPTIONS":
        if not is_authorized(request):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
    return await call_next(request)


class LoginBody(BaseModel):
    password: str


@app.post("/api/auth/login")
def login(body: LoginBody, response: Response):
    if not settings.password or body.password != settings.password:
        return JSONResponse({"detail": "wrong password"}, status_code=401)
    response.set_cookie(SESSION_COOKIE, session_token(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 90)
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    return {"auth_required": auth_enabled(), "authorized": is_authorized(request)}


app.include_router(assets.router, prefix="/api")
app.include_router(library.router, prefix="/api")
app.include_router(integrations.router, prefix="/api")


# ------------------------------------------------------------------ web UI
_dist = settings.frontend_dir
if (_dist / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="ui-assets")


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str):
    if full_path.startswith("api/"):
        return JSONResponse({"detail": "not found"}, status_code=404)
    candidate = (_dist / full_path).resolve()
    if full_path and candidate.is_file() and str(candidate).startswith(str(_dist.resolve())):
        return FileResponse(candidate)
    index = _dist / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"detail": "web UI not built - run `npm run build` in frontend/"}, status_code=404)
