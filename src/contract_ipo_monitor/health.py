from __future__ import annotations

from datetime import UTC, datetime
from threading import Lock
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .db import Database
from .observability import collector_states, dashboard_html, recent_alerts, recent_candidates


class HealthRegistry:
    def __init__(self):
        self._lock = Lock()
        self._database_ready = False
        self._collectors: dict[str, dict[str, Any]] = {}

    def set_database_ready(self, ready: bool) -> None:
        with self._lock:
            self._database_ready = ready

    def mark_success(self, name: str) -> None:
        with self._lock:
            self._collectors[name] = {
                "ok": True,
                "last_success_at": datetime.now(UTC).isoformat(),
                "error": None,
                "disabled": False,
            }

    def mark_error(self, name: str, error: str, *, disabled: bool = False) -> None:
        with self._lock:
            self._collectors[name] = {
                "ok": False,
                "last_success_at": self._collectors.get(name, {}).get("last_success_at"),
                "error": error,
                "disabled": disabled,
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            required = ("sec", "usaspending")
            ready = self._database_ready and all(self._collectors.get(name, {}).get("ok") for name in required)
            return {
                "live": True,
                "ready": ready,
                "database_ready": self._database_ready,
                "collectors": dict(self._collectors),
            }


def create_health_app(registry: HealthRegistry, db: Database | None = None) -> FastAPI:
    app = FastAPI(title="Government Contract IPO Monitor", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz():
        return registry.snapshot()

    @app.get("/readyz")
    def readyz():
        payload = registry.snapshot()
        return JSONResponse(payload, status_code=200 if payload["ready"] else 503)

    if db is not None:
        @app.get("/api/candidates")
        def candidates(limit: int = Query(50, ge=1, le=500)):
            return {"candidates": recent_candidates(db, limit=limit)}

        @app.get("/api/rejections")
        def rejections(limit: int = Query(50, ge=1, le=500)):
            return {"rejections": recent_candidates(db, limit=limit, rejected_only=True)}

        @app.get("/api/alerts")
        def alerts(limit: int = Query(50, ge=1, le=500)):
            return {"alerts": recent_alerts(db, limit=limit)}

        @app.get("/api/collectors")
        def collectors():
            return {"collectors": collector_states(db)}

        @app.get("/dashboard", response_class=HTMLResponse)
        def dashboard(limit: int = Query(50, ge=1, le=500)):
            return HTMLResponse(dashboard_html(db, limit=limit))

    return app
