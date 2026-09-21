"""Pluggable auth strategies for the generic HTTP(S) transport.

An AuthStrategy's only job is to take an outgoing request's kwargs (as
passed to ``requests``) and a resolved :class:`SecretBundle`, and return
request kwargs with auth applied -- it never sees the raw secret_ref, and
callers must not log the returned kwargs (they now contain the credential).
"""

from __future__ import annotations

import abc

from providerharvest_service.engine.secrets.base import SecretBundle


class AuthStrategy(abc.ABC):
    #: config key under ``auth_type`` this strategy handles, e.g. "api_key"
    name: str

    @abc.abstractmethod
    def apply(self, request_kwargs: dict, secret: SecretBundle, auth_opts: dict) -> dict:
        """Return a NEW dict of request kwargs with auth applied.

        ``request_kwargs`` follows ``requests``' call signature shape:
        keys like ``headers`` and ``params`` (dicts) may already be present
        and must be merged into, not overwritten.
        """

    def close(self) -> None:
        """Optional cleanup hook, called by the transport when it closes.
        A no-op for strategies that don't hold any resources (the
        default, and every strategy except MTLSAuth, which uses this to
        remove the temp files it writes client cert/key material to)."""
