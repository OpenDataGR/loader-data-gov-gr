"""ASGI entrypoint.

Started deliberately minimal (just a health check, to prove the deploy
pipeline end-to-end before anything real was built on top of it) and
is growing from there -- see admin_ftp.py for the first admin-panel
feature (FTP-push account/IP-whitelist management), and /me below for
the first endpoint that exercises the full per-user-identity chain:
Keycloak-JWT auth -> token provisioning -> a real CKAN Action API call
made as that specific person, not a service account.

Run directly with ``providerharvest-service`` (see pyproject.toml's
[project.scripts]) or ``uvicorn providerharvest_service.app:app``.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status

from providerharvest_service import db, harvest_db, scheduler
from providerharvest_service.admin_auth import require_admin_key
from providerharvest_service.admin_ftp import router as ftp_accounts_router
from providerharvest_service.admin_ftp import sync_router as ftp_accounts_sync_router
from providerharvest_service.admin_session import AdminSessionMiddleware
from providerharvest_service.auth import require_current_username
from providerharvest_service.ckan_tokens import get_or_provision_ckan_client
from providerharvest_service.db import get_session
from providerharvest_service.ftp_relay_ingest import router as ftp_relay_ingest_router
from providerharvest_service.proxy import router as proxy_router
from providerharvest_service.push_ingest import router as push_ingest_router
from providerharvest_service.ui_admin import router as admin_sources_ui_router
from providerharvest_service.ui_admin_ftp import router as admin_ftp_ui_router
from providerharvest_service.ui_admin_login import router as admin_login_ui_router
from providerharvest_service.ui_provider import router as provider_sources_ui_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Starts the recurring-harvest scheduler's background thread once
    the app is actually serving, not at import time -- so importing
    this module (e.g. from a test) never has the side effect of
    spawning it. A bare ``TestClient(app)`` (no ``with`` block) never
    triggers ASGI lifespan at all, which is exactly why every existing
    test in this repo can construct the real ``app`` without a real
    harvest-scheduler thread running underneath it.
    """
    thread = scheduler.start_background_thread(db.SessionLocal, harvest_db.get_session_factory)
    yield
    if thread is not None:
        thread.stop_event.set()
        thread.join(timeout=5)


app = FastAPI(title="providerharvest-service", lifespan=lifespan)
app.add_middleware(AdminSessionMiddleware)
app.include_router(ftp_accounts_router)
app.include_router(ftp_accounts_sync_router)
app.include_router(provider_sources_ui_router)
app.include_router(admin_sources_ui_router)
app.include_router(admin_ftp_ui_router)
app.include_router(admin_login_ui_router)
app.include_router(proxy_router)
app.include_router(push_ingest_router)
app.include_router(ftp_relay_ingest_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/me")
def me(
    username: str = Depends(require_current_username),
    session=Depends(get_session),
) -> dict:
    """Proves the whole per-user-identity chain end-to-end: who is
    this (Keycloak JWT) -> get/mint their own CKAN token -> a real
    CKAN Action API call made AS them, not a service account -- which
    organizations can they register a provider source for. This is
    the exact authorization check the per-user-token design exists
    for (see ckan_tokens.py's own docstring): CKAN's own permission
    system answers it, not this service's own judgment.
    """
    client = get_or_provision_ckan_client(session, username)
    orgs = client.call("organization_list_for_user", {"id": username, "permission": "admin"})
    return {"username": username, "admin_of_orgs": [org["name"] for org in orgs]}


@app.get("/admin/harvest-db/health", dependencies=[Depends(require_admin_key)])
def harvest_db_health() -> dict:
    """Proves the harvested-data PostgreSQL connection end-to-end -- a
    real round-trip query, not just 'the env var is set' -- before any
    real schema/scheduler gets built on top of it. Deliberately its own
    endpoint rather than folded into /health: that one is used by
    deploy.yml's own smoke test, and coupling it to this new dependency
    would turn a Postgres blip into a false-positive failed deploy for
    changes that have nothing to do with it.
    """
    try:
        harvest_db.check_connectivity()
    except Exception as exc:  # noqa: BLE001 -- shown to the admin verbatim, this IS the diagnostic
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "harvest DB unreachable: %s" % exc) from exc
    return {"status": "ok"}


def main() -> None:
    host = os.environ.get("PROVIDERHARVEST_HOST", "127.0.0.1")
    port = int(os.environ.get("PROVIDERHARVEST_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
