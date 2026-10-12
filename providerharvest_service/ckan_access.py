"""Who may use the loader, according to CKAN: a CKAN sysadmin, or an administrator of at least one organization.

Keycloak says *who* a person is; it holds no CKAN permissions (the ``ckan`` client there has no roles, mappers or
groups). Those live in CKAN, so the loader asks CKAN, with its own sysadmin key (``CKAN_BASE_URL`` /
``CKAN_SYSADMIN_API_TOKEN``).

Two facts about the real data.gov.gr CKAN, both checked against it rather than assumed:
* a person's CKAN user is named ``user_<their Keycloak user id>`` (the ID token's ``sub``) -- never matched by user
  name, because a *different* CKAN user could have the same name;
* ``organization_list_for_user(permission="admin")`` returns the organizations the person administers and is empty for
  an editor or a member, whereas ``manage_group``, ``create_dataset`` and ``read`` let editors through too. (The role
  names in ``member_list`` come back translated -- Greek -- so they are not used.)

It fails closed: when CKAN cannot be asked, nobody is let in through this rule.
"""

from __future__ import annotations

import dataclasses
import logging
import os

from providerharvest_service import ckan_client

logger = logging.getLogger(__name__)

USERNAME_PREFIX = "user_"


class CkanAccessError(Exception):
    """The question could not be answered; the message is fit to show the person."""


@dataclasses.dataclass(frozen=True)
class Access:
    allowed: bool
    ckan_user: str
    sysadmin: bool = False
    organizations: tuple[str, ...] = ()
    reason: str = ""


def configured() -> bool:
    return bool(os.environ.get("CKAN_BASE_URL") and os.environ.get("CKAN_SYSADMIN_API_TOKEN"))


def ckan_username(keycloak_subject: str) -> str:
    return USERNAME_PREFIX + keycloak_subject


def _client() -> ckan_client.CKANActionClient:  # replaced in tests
    return ckan_client.CKANActionClient(os.environ["CKAN_BASE_URL"], os.environ["CKAN_SYSADMIN_API_TOKEN"], timeout=15.0)


def check(keycloak_subject: str) -> Access:
    """Whether CKAN says this person may use the loader. Raises ``CkanAccessError`` when CKAN cannot be asked."""
    if not keycloak_subject:
        raise CkanAccessError("The login did not say who you are.")
    if not configured():
        raise CkanAccessError("The CKAN connection is not set up on this deployment.")
    name = ckan_username(keycloak_subject)
    client = _client()
    try:
        user = client.call("user_show", {"id": name})
        if user.get("state") not in (None, "active"):
            return Access(False, name, reason="Your CKAN account is not active.")
        if user.get("sysadmin"):
            return Access(True, name, sysadmin=True)
        orgs = client.call("organization_list_for_user", {"id": name, "permission": "admin"}) or []
    except ckan_client.CKANNotFound:
        return Access(False, name, reason="You have no account in CKAN yet. Sign in to data.gov.gr once, then try again.")
    except ckan_client.CKANActionError as exc:
        logger.warning("ckan access check for %s failed: %s", name, exc)
        raise CkanAccessError("CKAN could not be asked who you are (%s). Try again in a moment." % exc) from exc
    names = tuple(str(o.get("name")) for o in orgs if isinstance(o, dict) and o.get("name"))
    if not names:
        return Access(False, name, reason="You are not an administrator of any organization in CKAN.")
    return Access(True, name, organizations=names)
