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

import uvicorn
from fastapi import Depends, FastAPI

from providerharvest_service.admin_ftp import router as ftp_accounts_router
from providerharvest_service.auth import require_current_username
from providerharvest_service.ckan_tokens import get_or_provision_ckan_client
from providerharvest_service.db import get_session
from providerharvest_service.proxy import router as proxy_router
from providerharvest_service.ui_admin import router as admin_sources_ui_router
from providerharvest_service.ui_provider import router as provider_sources_ui_router

app = FastAPI(title="providerharvest-service")
app.include_router(ftp_accounts_router)
app.include_router(provider_sources_ui_router)
app.include_router(admin_sources_ui_router)
app.include_router(proxy_router)


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


def main() -> None:
    host = os.environ.get("PROVIDERHARVEST_HOST", "127.0.0.1")
    port = int(os.environ.get("PROVIDERHARVEST_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
