"""Login/logout for the admin HTML UI's session gate. The gate itself
is enforced by ``admin_session.AdminSessionMiddleware``, not anything
in this module -- this is just the form and the two actions
(login/logout) that create or clear the signed session cookie it
checks."""

from __future__ import annotations

import datetime
import logging
from urllib.parse import quote, urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from starlette.concurrency import run_in_threadpool

from providerharvest_service import admin_session, build_info, failed_auth, nav, oidc_login

router = APIRouter(prefix="/admin", tags=["admin-login"])
auth_router = APIRouter(prefix="/auth", tags=["oidc-login"])
templates = Jinja2Templates(directory="providerharvest_service/templates")
templates.env.globals["build_info"] = build_info.get_build_info()
templates.env.globals["changelog"] = build_info.get_changelog()
templates.env.globals["now"] = datetime.datetime.now
templates.env.globals["pending_approvals"] = nav.pending_approvals

logger = logging.getLogger(__name__)

_DEFAULT_NEXT = "/overview"


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
            "oidc_enabled": oidc_login.enabled(),
            "password_login": oidc_login.password_login_allowed() or not oidc_login.enabled(),
        },
    )


@router.post("/login")
async def login_submit(request: Request):
    if oidc_login.enabled() and not oidc_login.password_login_allowed():
        return RedirectResponse("/admin/login?flash=Please+sign+in+with+data.gov.gr", status_code=303)
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
        await run_in_threadpool(failed_auth.record, request, failed_auth.WEB_LOGIN, str(username), 401)
        return RedirectResponse(
            "/admin/login?next=%s&flash=Invalid+username+or+password" % quote(next_url, safe=""), status_code=303
        )

    # The shared account is the break-glass login of the whole site, so it counts as a super admin.
    return _start_session(next_url, username, {"u": str(username), "via": "Shared account", "super": True})


def _start_session(next_url: str, username: str, profile: dict) -> RedirectResponse:
    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(admin_session.PROFILE_COOKIE, admin_session.seal("profile", profile), max_age=admin_session.DEFAULT_TTL_S, httponly=True, secure=True, samesite="lax")
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
def logout(request: Request) -> RedirectResponse:
    # Ends this site's session, and Keycloak's too when the person came in through it -- otherwise "log out"
    # followed by "sign in" would silently sign them straight back in.
    target = oidc_login.logout_url(str(request.base_url), request.cookies.get(oidc_login.ID_COOKIE, "")) if oidc_login.enabled() else None
    response = RedirectResponse(target or "/admin/login", status_code=303)
    response.delete_cookie(admin_session.COOKIE_NAME)
    response.delete_cookie(oidc_login.ID_COOKIE)
    response.delete_cookie(admin_session.PROFILE_COOKIE)
    return response


# -- Keycloak (data.gov.gr) login -----------------------------------------------------------------------------------------------------

def _back_to_login(message: str, next_url: str = "") -> RedirectResponse:
    query = {"flash": message}
    if next_url:
        query["next"] = next_url
    return RedirectResponse("/admin/login?" + urlencode(query), status_code=303)


@auth_router.get("/login")
def oidc_start(request: Request):
    if not oidc_login.enabled():
        return RedirectResponse("/admin/login", status_code=303)
    next_url = _safe_next(request.query_params.get("next"))
    try:
        url, cookie = oidc_login.start(str(request.base_url), next_url)
    except oidc_login.OidcError as exc:
        return _back_to_login(str(exc), next_url)
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(oidc_login.TX_COOKIE, cookie, max_age=oidc_login.TX_TTL_S, httponly=True, secure=True, samesite="lax", path="/auth")
    return response


@auth_router.get("/callback")
async def oidc_callback(request: Request):
    if not oidc_login.enabled():
        return RedirectResponse("/admin/login", status_code=303)
    try:
        username, next_url, id_token, profile = await run_in_threadpool(
            oidc_login.finish, str(request.base_url), request.cookies.get(oidc_login.TX_COOKIE, ""), dict(request.query_params)
        )
    except oidc_login.NotAllowed as exc:
        await run_in_threadpool(failed_auth.record, request, failed_auth.WEB_LOGIN, exc.username, 403)
        logger.warning("oidc: %s signed in at Keycloak but is not allowed: %s", exc.username, exc.reason)
        response = _back_to_login(str(exc))
    except oidc_login.OidcError as exc:
        logger.warning("oidc: sign-in failed: %s", exc)
        response = _back_to_login(str(exc))
    else:
        logger.info("oidc: %s signed in", username)
        response = _start_session(_safe_next(next_url), username, profile)
        response.set_cookie(oidc_login.ID_COOKIE, id_token, max_age=admin_session.DEFAULT_TTL_S, httponly=True, secure=True, samesite="lax")
    response.delete_cookie(oidc_login.TX_COOKIE, path="/auth")
    return response
