"""Per-user CKAN API token provisioning.

The "per-user token does both" design: mint a personal CKAN API token
for a person once (via a standing sysadmin credential, used for
nothing else), store it envelope-encrypted at rest (the exact same
AES-256-GCM scheme already built for provider credentials -- see
engine/secrets/envelope.py -- reused here rather than inventing a
second secret store), and reuse that SAME per-user token for both the
org-membership authorization check and every subsequent write. CKAN's
own permission system then enforces org membership for real on every
call, rather than this service's own logic being the only thing
standing between a caller and writing to the wrong organization.

Minted lazily, at first actual need -- not preemptively for every
person in the realm, not re-minted on every request.
"""

from __future__ import annotations

import os

from sqlalchemy.orm import Session

from providerharvest_service.ckan_client import CKANActionClient
from providerharvest_service.engine.secrets.envelope import EnvelopeSecretsBackend
from providerharvest_service.models import CkanUserToken


class CkanTokenProvisioningError(RuntimeError):
    pass


class CkanUserTokenRepository:
    """Implements engine.secrets.envelope.SecretRepository against
    CkanUserToken. That protocol's ``harvest_source_id``/``secret_ref``
    vocabulary is just an opaque identifier -- here it's always a CKAN
    username, and the username itself doubles as the row's primary key
    and the "secret_ref", so there's no separate opaque ref to track.
    """

    def __init__(self, session: Session):
        self._session = session

    def create(self, harvest_source_id: str, ciphertext: bytes,
               data_key_wrapped: bytes, algorithm: str) -> str:
        username = harvest_source_id
        row = self._session.get(CkanUserToken, username)
        if row is None:
            row = CkanUserToken(username=username)
            self._session.add(row)
        row.ciphertext = ciphertext
        row.data_key_wrapped = data_key_wrapped
        row.algorithm = algorithm
        self._session.commit()
        return username

    def get(self, secret_ref: str) -> dict:
        row = self._session.get(CkanUserToken, secret_ref)
        if row is None:
            raise LookupError("no stored CKAN token for %r" % secret_ref)
        return {
            "ciphertext": row.ciphertext,
            "data_key_wrapped": row.data_key_wrapped,
            "algorithm": row.algorithm,
            "harvest_source_id": row.username,
            "version": 1,
        }

    def update(self, secret_ref: str, ciphertext: bytes, data_key_wrapped: bytes) -> None:
        row = self._session.get(CkanUserToken, secret_ref)
        if row is None:
            raise LookupError("no stored CKAN token for %r" % secret_ref)
        row.ciphertext = ciphertext
        row.data_key_wrapped = data_key_wrapped
        self._session.commit()

    def delete(self, secret_ref: str) -> None:
        row = self._session.get(CkanUserToken, secret_ref)
        if row is not None:
            self._session.delete(row)
            self._session.commit()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CkanTokenProvisioningError(
            "%s is not configured on this deployment" % name
        )
    return value


def _sysadmin_client() -> CKANActionClient:
    return CKANActionClient(_required_env("CKAN_BASE_URL"), _required_env("CKAN_SYSADMIN_API_TOKEN"))


def get_or_provision_ckan_client(
    session: Session,
    username: str,
    *,
    sysadmin_client: CKANActionClient | None = None,
) -> CKANActionClient:
    """A CKANActionClient bound to ``username``'s own personal CKAN API
    token -- minting and encrypting-at-rest one via the standing
    sysadmin credential if this is the first time this particular
    person has needed one, otherwise decrypting and reusing the
    already-stored one.

    ``sysadmin_client`` is accepted purely so tests can inject a fake
    rather than needing real env vars or a real CKAN to talk to; real
    callers omit it and the standing sysadmin credential (CKAN_BASE_URL
    / CKAN_SYSADMIN_API_TOKEN) is used.
    """
    base_url = _required_env("CKAN_BASE_URL")
    backend = EnvelopeSecretsBackend(CkanUserTokenRepository(session))

    try:
        bundle = backend.get(username)
        return CKANActionClient(base_url, bundle.fields["api_token"])
    except LookupError:
        pass

    sysadmin = sysadmin_client or _sysadmin_client()
    result = sysadmin.call("api_token_create", {
        "user": username,
        "name": "loader-data-gov-gr",
    })
    raw_token = result["token"]

    backend.put(username, {"api_token": raw_token})
    return CKANActionClient(base_url, raw_token)
