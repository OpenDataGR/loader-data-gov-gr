"""Things the masthead needs on every page, not just the page that owns them.

``pending_approvals`` is the number on the *Admin dashboard* tab: how many
registered sources are waiting for an admin to approve them. It is a Jinja
global (see each ``ui_*`` module's ``templates.env.globals``) so every page
that extends ``base.html`` can show it without each route having to pass it.

It reads through ``db.SessionLocal`` -- not the request's ``get_session``
dependency, which a template global cannot see -- and never raises: a database
hiccup must cost a page its badge, not the page.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from providerharvest_service import db
from providerharvest_service.models import ProviderSource

logger = logging.getLogger(__name__)


def pending_approvals() -> int:
    try:
        with db.SessionLocal() as session:
            return session.scalar(
                select(func.count()).select_from(ProviderSource).where(ProviderSource.status == "pending")
            ) or 0
    except Exception:  # noqa: BLE001 -- see module docstring
        logger.exception("could not count the sources awaiting approval")
        return 0
