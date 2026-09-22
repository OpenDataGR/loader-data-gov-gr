"""The one deliberate integration point with CKAN: a ``get_action``-shaped
HTTP adapter over CKAN's public Action API, matching
``ckan.plugins.toolkit.get_action``'s own call shape so the ported
loaders (``engine/loaders/*.py``, written against that shape while still
part of the CKAN plugin) work against this unchanged. Nothing under
``engine/`` imports this module or anything from ``requests`` about
CKAN specifically -- this is the only file in the service that knows
CKAN's Action API exists at all.

One ``CKANActionClient`` instance is bound to exactly one CKAN API
token, i.e. one identity. Per the "per-user token does both" design:
construct one with the CALLER's own per-user token (minted once via a
sysadmin credential at provisioning time, never the sysadmin token
itself) and use that same instance for both the authorization check
(``organization_list_for_user``) and every subsequent write -- CKAN's
own permission system then enforces org membership on every call for
real, instead of this service's own logic being the only thing
standing between a caller and writing to the wrong organization.
"""

from __future__ import annotations

from typing import Any, Callable

import requests


class CKANActionError(Exception):
    """Base class for every error CKAN's Action API reports back.
    ``ckan_type``/``details`` carry through whatever CKAN itself sent
    (its own ``error.__type`` and the rest of the error dict -- e.g. a
    Validation Error's per-field messages), so callers that need to
    inspect a specific field still can without this adapter having to
    re-model CKAN's entire error shape up front."""

    def __init__(self, action_name: str, message: str, *,
                 ckan_type: str | None = None, details: dict | None = None,
                 status_code: int | None = None):
        super().__init__("%s: %s" % (action_name, message))
        self.action_name = action_name
        self.ckan_type = ckan_type
        self.details = details or {}
        self.status_code = status_code


class CKANValidationError(CKANActionError):
    pass


class CKANNotAuthorized(CKANActionError):
    pass


class CKANNotFound(CKANActionError):
    pass


# Keyed off CKAN's own error.__type, which is stable across CKAN
# versions (it's derived from the raised exception's class name on the
# CKAN side, e.g. ckan.logic.ValidationError -> "Validation Error").
_ERROR_TYPES: dict[str, type[CKANActionError]] = {
    "Validation Error": CKANValidationError,
    "Authorization Error": CKANNotAuthorized,
    "Not Found Error": CKANNotFound,
}


class CKANActionClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0,
                 session: requests.Session | None = None):
        self._api_base = base_url.rstrip("/") + "/api/3/action"
        self._token = token
        self._timeout = timeout
        self._session = session or requests.Session()

    def get_action(self, action_name: str) -> Callable[[dict, Any], Any]:
        """Matches ``toolkit.get_action``'s own signature -- ``context``
        is accepted for that shape compatibility only. There's nothing
        to switch on inside it here: this client already IS one specific
        identity's session (see the class docstring), CKAN's in-process
        notion of a context-supplied ``user``/``ignore_auth`` doesn't
        carry over to an HTTP caller."""
        def _call(context: dict, data_dict: dict) -> Any:
            return self.call(action_name, data_dict)
        return _call

    def call(self, action_name: str, data_dict: dict) -> Any:
        try:
            response = self._session.post(
                "%s/%s" % (self._api_base, action_name),
                json=data_dict,
                headers={"Authorization": self._token},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise CKANActionError(action_name, "request failed: %s" % exc) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise CKANActionError(
                action_name,
                "non-JSON response (HTTP %s)" % response.status_code,
                status_code=response.status_code,
            ) from exc

        if payload.get("success"):
            return payload.get("result")

        error = payload.get("error") or {}
        ckan_type = error.get("__type")
        message = error.get("message") or str(error) or "action failed"
        error_cls = _ERROR_TYPES.get(ckan_type, CKANActionError)
        raise error_cls(
            action_name, message, ckan_type=ckan_type, details=error,
            status_code=response.status_code,
        )
