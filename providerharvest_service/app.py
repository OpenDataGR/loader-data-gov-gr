"""ASGI entrypoint.

Started deliberately minimal (just a health check, to prove the deploy
pipeline end-to-end before anything real was built on top of it) and
is growing the admin API from there -- see admin_ftp.py for the first
real feature (FTP-push account/IP-whitelist management).

Run directly with ``providerharvest-service`` (see pyproject.toml's
[project.scripts]) or ``uvicorn providerharvest_service.app:app``.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

from providerharvest_service.admin_ftp import router as ftp_accounts_router

app = FastAPI(title="providerharvest-service")
app.include_router(ftp_accounts_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def main() -> None:
    host = os.environ.get("PROVIDERHARVEST_HOST", "127.0.0.1")
    port = int(os.environ.get("PROVIDERHARVEST_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
