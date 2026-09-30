"""Login/logout for the admin HTML UI's session gate. The gate itself
is enforced by ``admin_session.AdminSessionMiddleware``, not anything
in this module -- this is just the form and the two actions
(login/logout) that create or clear the signed session cookie it
checks."""

from __future__ import annotations

import datetime
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from providerharvest_service import admin_session, build_info

router = APIRouter(prefix="/admin", tags=["admin-login"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now

_DEFAULT_NEXT = "/provider/sources"


def _safe_next(value: str | None) -> str:
    # Only ever redirects to a path on this same site -- `next` is
    # client-supplied (a query param/hidden form field), so an
    # unvalidated redirect there would be an open-redirect vector, same
    # concern ui_admin.py's own `_came_from` already guards against. The
    # whole site is behind the login now, so any local path is fair game;
    # "//host" and "/\\host" are how a *relative-looking* value is
    # smuggled off-site, so they're refused too.
    if not value:
        return _DEFAULT_NEXT
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith(("//", "/\\")):
        return _DEFAULT_NEXT
    return value


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {
            "next": _safe_next(request.query_params.get("next")),
            "flash": request.query_params.get("flash"),
        },
    )


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    next_url = _safe_next(form.get("next"))

    try:
        ok = admin_session.verify_credentials(username, password)
    except admin_session.SessionConfigError:
        return RedirectResponse(
            "/admin/login?flash=Admin+login+is+not+configured+on+this+deployment", status_code=303
        )

    if not ok:
        return RedirectResponse(
            "/admin/login?next=%s&flash=Invalid+username+or+password" % quote(next_url, safe=""), status_code=303
        )

    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        admin_session.COOKIE_NAME,
        admin_session.create_session_cookie(username),
        max_age=admin_session.DEFAULT_TTL_S,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(admin_session.COOKIE_NAME)
    return response
