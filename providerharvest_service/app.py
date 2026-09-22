"""ASGI entrypoint. Deliberately minimal right now: a health check is
the first thing a deploy pipeline needs to prove end-to-end (reachable
through the Application Gateway, running under systemd, restarted
cleanly on a new deploy) before any real endpoint (register/
test-connection/list/approve/...) is built on top of infrastructure
that hasn't been verified yet.

Run directly with ``providerharvest-service`` (see pyproject.toml's
[project.scripts]) or ``uvicorn providerharvest_service.app:app``.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

app = FastAPI(title="providerharvest-service")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def main() -> None:
    host = os.environ.get("PROVIDERHARVEST_HOST", "127.0.0.1")
    port = int(os.environ.get("PROVIDERHARVEST_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
