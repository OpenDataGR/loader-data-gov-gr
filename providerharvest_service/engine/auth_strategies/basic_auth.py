from __future__ import annotations

from providerharvest_service.engine.auth_strategies.base import AuthStrategy
from providerharvest_service.engine.secrets.base import SecretBundle


class BasicAuth(AuthStrategy):
    """HTTP Basic authentication (RFC 7617).

    Uses ``requests``' own ``auth`` kwarg rather than hand-building the
    ``Authorization: Basic ...`` header -- it already gets the encoding
    edge cases (non-ASCII credentials, etc.) right.

    ``credential_fields`` (secret): username, password.
    """

    name = "basic_auth"

    def apply(self, request_kwargs: dict, secret: SecretBundle, auth_opts: dict) -> dict:
        username = secret.fields.get("username")
        password = secret.fields.get("password")
        if not username or password is None:
            raise ValueError(
                "Secret bundle for basic_auth is missing 'username' and/or 'password'"
            )
        kwargs = dict(request_kwargs)
        kwargs["auth"] = (username, password)
        return kwargs
