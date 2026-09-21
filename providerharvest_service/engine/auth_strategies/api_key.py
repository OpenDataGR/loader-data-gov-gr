from __future__ import annotations

from providerharvest_service.engine.auth_strategies.base import AuthStrategy
from providerharvest_service.engine.secrets.base import SecretBundle


class ApiKeyAuth(AuthStrategy):
    """Injects an API key as a header (default) or query parameter.

    ``auth_opts`` (non-secret, lives in HarvestSource.config):
        location: "header" | "query"   (default "header")
        name: the header or query-param name (default "X-API-Key")
    """

    name = "api_key"

    def apply(self, request_kwargs: dict, secret: SecretBundle, auth_opts: dict) -> dict:
        location = (auth_opts or {}).get("location", "header")
        param_name = (auth_opts or {}).get("name", "X-API-Key")
        api_key = secret.fields.get("api_key")
        if not api_key:
            raise ValueError("Secret bundle for api_key auth is missing 'api_key'")

        kwargs = dict(request_kwargs)
        if location == "header":
            headers = dict(kwargs.get("headers") or {})
            headers[param_name] = api_key
            kwargs["headers"] = headers
        elif location == "query":
            params = dict(kwargs.get("params") or {})
            params[param_name] = api_key
            kwargs["params"] = params
        else:
            raise ValueError("Unknown api_key location %r" % location)
        return kwargs
