"""OAuth2 client-credentials grant (RFC 6749 section 4.4) -- fetches (and
caches until shortly before expiry) a bearer access token from the
provider's own token endpoint, then applies it as a standard
``Authorization: Bearer`` header. Only the client-credentials grant is
supported: it's the one that makes sense for an unattended scheduled
harvester -- there's no human present for a redirect-based flow.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import requests

from providerharvest_service.engine.auth_strategies.base import AuthStrategy
from providerharvest_service.engine.validators import assert_safe_http_url
from providerharvest_service.engine.secrets.base import SecretBundle

#: Refresh this many seconds before the token's reported expiry, so a
#: request that starts right at the boundary doesn't race an already-
#: expired token.
EXPIRY_SAFETY_MARGIN_S = 30
DEFAULT_TIMEOUT_S = 30
DEFAULT_TOKEN_LIFETIME_S = 300  # used only if the provider omits expires_in


class OAuth2ClientCredentialsAuth(AuthStrategy):
    """``auth_opts`` (non-secret, lives in HarvestSource.config):
        token_url: the provider's OAuth2 token endpoint (required)
        scope: space-separated scopes to request (optional)
        ca_bundle_path: custom CA bundle for the token endpoint (optional)
        allow_private_ranges: same meaning as the transport's own flag (optional)

    ``credential_fields`` (secret): client_id, client_secret.

    Client authentication to the token endpoint uses HTTP Basic (the
    method RFC 6749 section 2.3.1 recommends), not a client_secret field
    in the POST body.
    """

    name = "oauth2_client_credentials"

    def __init__(self, *, session_factory: Callable[[], requests.Session] = requests.Session):
        self._session_factory = session_factory
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def apply(self, request_kwargs: dict, secret: SecretBundle, auth_opts: dict) -> dict:
        token = self._get_token(secret, auth_opts or {})
        kwargs = dict(request_kwargs)
        headers = dict(kwargs.get("headers") or {})
        headers["Authorization"] = "Bearer %s" % token
        kwargs["headers"] = headers
        return kwargs

    def _get_token(self, secret: SecretBundle, auth_opts: dict) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token

        token_url = auth_opts.get("token_url")
        if not token_url:
            raise ValueError("auth_opts.token_url is required for oauth2_client_credentials")
        client_id = secret.fields.get("client_id")
        client_secret = secret.fields.get("client_secret")
        if not client_id or not client_secret:
            raise ValueError(
                "Secret bundle for oauth2_client_credentials is missing 'client_id' "
                "and/or 'client_secret'"
            )

        # Same requirement as every other outbound network target this
        # extension talks to: the token endpoint is provider-supplied
        # config too, and re-validated on every fetch (not just once at
        # registration) for the same DNS-rebind reason DirectHTTPSTransport
        # re-validates its own base_url on every connect().
        assert_safe_http_url(
            token_url, allow_private_ranges=auth_opts.get("allow_private_ranges", False)
        )

        data = {"grant_type": "client_credentials"}
        if auth_opts.get("scope"):
            data["scope"] = auth_opts["scope"]

        session = self._session_factory()
        try:
            response = session.post(
                token_url,
                data=data,
                auth=(client_id, client_secret),
                verify=auth_opts.get("ca_bundle_path") or True,
                timeout=DEFAULT_TIMEOUT_S,
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            session.close()

        access_token = payload.get("access_token")
        if not access_token:
            raise ValueError("Token endpoint response did not include 'access_token'")

        expires_in = payload.get("expires_in", DEFAULT_TOKEN_LIFETIME_S)
        self._token = access_token
        self._expires_at = time.monotonic() + max(0, expires_in - EXPIRY_SAFETY_MARGIN_S)
        return access_token
