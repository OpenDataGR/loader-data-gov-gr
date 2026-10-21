"""The Alerts page and the little feed the open pages ask every few seconds (see ``alerts.py``). Behind the site-wide login.

A super admin sees every alert (including the FTP box's); anyone else sees those of the sources they may see -- the ones of the CKAN organizations they
administer, the same rule as the Overview -- and never the box's.
"""

from __future__ import annotations

import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from providerharvest_service import build_info, nav, overview, ui_provider
from providerharvest_service.db import get_session
from providerharvest_service.models import Alert

router = APIRouter(prefix="/alerts", tags=["alerts-ui"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now
templates.env.globals["pending_approvals"] = nav.pending_approvals
templates.env.filters["utc"] = lambda value: value.strftime("%Y-%m-%d %H:%M:%S") if value else ""
templates.env.filters["short_utc"] = lambda value: value.strftime("%m-%d %H:%M") if value else ""

RECENTLY_RESOLVED = datetime.timedelta(hours=24)
MAX_CHANGES = 8


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _may_see(request: Request, session: Session):
    """A function: may this person see this alert?"""
    is_super, ident = ui_provider._signed_in(request)
    if is_super:
        return lambda alert: True
    scope = overview.visible_sources(session, is_super=False, ckan_user=ident)
    ids = {s.id for s in scope.sources}
    return lambda alert: alert.source_id is not None and alert.source_id in ids


def _who(request: Request) -> str:
    user = getattr(request.state, "user", None) or {}
    return str(user.get("username") or "")


@router.get("/feed")
def feed(request: Request, since: str = "", session: Session = Depends(get_session)):
    """What the open pages ask: how many alerts are open and not yet acknowledged, and what changed since ``since`` (nothing on the first call, so a page
    that has just been opened does not shout about old news)."""
    now = _utcnow()
    may = _may_see(request, session)
    open_alerts = [a for a in session.scalars(select(Alert).where(Alert.resolved_at.is_(None))).all() if may(a)]
    changes = []
    if since:
        try:
            cursor = datetime.datetime.fromisoformat(since)
        except ValueError:
            cursor = now
        recent = session.scalars(select(Alert).where(Alert.changed_at > cursor).order_by(Alert.changed_at.asc()).limit(50)).all()
        for a in recent:
            if may(a):
                state = "resolved" if a.resolved_at else ("acknowledged" if a.acknowledged_by else "new")
                changes.append({"id": a.id, "state": state, "severity": a.severity, "title": a.title, "detail": a.detail[:300], "link": a.link or "/alerts"})
    return JSONResponse({"now": now.isoformat(), "open": len(open_alerts), "unacknowledged": sum(1 for a in open_alerts if not a.acknowledged_by),
                         "changes": changes[-MAX_CHANGES:]}, headers={"Cache-Control": "no-store"})


@router.get("", response_class=HTMLResponse)
def alerts_page(request: Request, session: Session = Depends(get_session)):
    may = _may_see(request, session)
    now = _utcnow()
    rows = session.scalars(select(Alert).order_by(Alert.first_seen.desc())).all()
    opened = sorted((a for a in rows if a.resolved_at is None and may(a)), key=lambda a: (a.acknowledged_by is not None, a.severity != "error", a.first_seen))
    resolved = [a for a in rows if a.resolved_at is not None and now - a.resolved_at < RECENTLY_RESOLVED and may(a)]
    return templates.TemplateResponse(request, "alerts/list.html", {
        "open": opened, "resolved": resolved, "flash": request.query_params.get("flash"), "flash_kind": request.query_params.get("flash_kind", "success"),
    })


def _go(message: str, kind: str = "success") -> RedirectResponse:
    return RedirectResponse("/alerts?%s" % urlencode({"flash": message, "flash_kind": kind}), status_code=303)


@router.post("/ack-all")
def acknowledge_all(request: Request, session: Session = Depends(get_session)):
    may = _may_see(request, session)
    now, who = _utcnow(), _who(request) or "someone"
    count = 0
    for a in session.scalars(select(Alert).where(Alert.resolved_at.is_(None), Alert.acknowledged_by.is_(None))).all():
        if may(a):
            a.acknowledged_by, a.acknowledged_at, a.changed_at = who, now, now
            count += 1
    session.commit()
    return _go("%d alert%s acknowledged." % (count, "" if count == 1 else "s"))


@router.post("/{alert_id}/ack")
def acknowledge(alert_id: str, request: Request, session: Session = Depends(get_session)):
    alert = session.get(Alert, alert_id)
    if alert is None or not _may_see(request, session)(alert):
        return _go("No such alert.", "error")
    if alert.acknowledged_by is None:
        alert.acknowledged_by, alert.acknowledged_at, alert.changed_at = _who(request) or "someone", _utcnow(), _utcnow()
        session.commit()
    return _go("Acknowledged. It stays listed until the problem is gone.")
