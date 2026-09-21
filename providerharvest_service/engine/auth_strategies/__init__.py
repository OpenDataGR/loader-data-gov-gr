"""Registry of every HTTP AuthStrategy, keyed by the ``auth_type`` config
value -- shared by harvesters/base_generic.py (real harvest runs) and
logic/action.py (the "test connection" dry run), so both dispatch the
same way instead of one of them drifting out of sync with what's
actually implemented. Deliberately CKAN-independent (no ``ckan`` import
here), matching the rest of this package -- callers translate
``ValueError`` into whatever error type fits their own layer.
"""

from __future__ import annotations

from providerharvest_service.engine.auth_strategies.api_key import ApiKeyAuth
from providerharvest_service.engine.auth_strategies.base import AuthStrategy
from providerharvest_service.engine.auth_strategies.basic_auth import BasicAuth
from providerharvest_service.engine.auth_strategies.mtls import MTLSAuth
from providerharvest_service.engine.auth_strategies.oauth2_client_credentials import (
    OAuth2ClientCredentialsAuth,
)

AUTH_STRATEGIES: dict[str, type[AuthStrategy]] = {
    "api_key": ApiKeyAuth,
    "basic_auth": BasicAuth,
    "oauth2_client_credentials": OAuth2ClientCredentialsAuth,
    "mtls": MTLSAuth,
}


def build_auth_strategy(auth_type: str) -> AuthStrategy:
    strategy_cls = AUTH_STRATEGIES.get(auth_type)
    if strategy_cls is None:
        raise ValueError(
            "auth_type %r is not yet implemented (available: %s)"
            % (auth_type, sorted(AUTH_STRATEGIES))
        )
    return strategy_cls()
