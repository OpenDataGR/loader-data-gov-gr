import os

import pytest

from providerharvest_service.engine.auth_strategies.api_key import ApiKeyAuth
from providerharvest_service.engine.auth_strategies.basic_auth import BasicAuth
from providerharvest_service.engine.auth_strategies.mtls import MTLSAuth
from providerharvest_service.engine.auth_strategies.oauth2_client_credentials import (
    OAuth2ClientCredentialsAuth,
)
from providerharvest_service.engine.secrets.base import SecretBundle


def test_api_key_header_default():
    kwargs = ApiKeyAuth().apply({}, SecretBundle(fields={"api_key": "abc123"}), {})
    assert kwargs["headers"] == {"X-API-Key": "abc123"}


def test_api_key_custom_header_name():
    kwargs = ApiKeyAuth().apply(
        {}, SecretBundle(fields={"api_key": "abc123"}), {"name": "Authorization"}
    )
    assert kwargs["headers"] == {"Authorization": "abc123"}


def test_api_key_query_param():
    kwargs = ApiKeyAuth().apply(
        {}, SecretBundle(fields={"api_key": "abc123"}), {"location": "query", "name": "token"}
    )
    assert kwargs["params"] == {"token": "abc123"}


def test_api_key_merges_with_existing_headers():
    kwargs = ApiKeyAuth().apply(
        {"headers": {"Accept": "application/json"}},
        SecretBundle(fields={"api_key": "abc123"}),
        {},
    )
    assert kwargs["headers"] == {"Accept": "application/json", "X-API-Key": "abc123"}


def test_api_key_missing_raises():
    with pytest.raises(ValueError):
        ApiKeyAuth().apply({}, SecretBundle(fields={}), {})


def test_api_key_does_not_mutate_input_kwargs():
    original = {"headers": {"Accept": "application/json"}}
    ApiKeyAuth().apply(original, SecretBundle(fields={"api_key": "abc123"}), {})
    assert original == {"headers": {"Accept": "application/json"}}


# -- BasicAuth ---------------------------------------------------------

def test_basic_auth_sets_auth_tuple():
    kwargs = BasicAuth().apply(
        {}, SecretBundle(fields={"username": "alice", "password": "s3cret"}), {}
    )
    assert kwargs["auth"] == ("alice", "s3cret")


def test_basic_auth_allows_empty_password():
    # An empty (but present) password is a legitimate credential shape
    # for some providers -- only a genuinely missing password should raise.
    kwargs = BasicAuth().apply(
        {}, SecretBundle(fields={"username": "alice", "password": ""}), {}
    )
    assert kwargs["auth"] == ("alice", "")


def test_basic_auth_missing_username_raises():
    with pytest.raises(ValueError):
        BasicAuth().apply({}, SecretBundle(fields={"password": "s3cret"}), {})


def test_basic_auth_missing_password_raises():
    with pytest.raises(ValueError):
        BasicAuth().apply({}, SecretBundle(fields={"username": "alice"}), {})


def test_basic_auth_does_not_mutate_input_kwargs():
    original = {"headers": {"Accept": "application/json"}}
    BasicAuth().apply(original, SecretBundle(fields={"username": "a", "password": "b"}), {})
    assert original == {"headers": {"Accept": "application/json"}}


# -- OAuth2ClientCredentialsAuth ----------------------------------------

class _FakeTokenResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body or {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)


class _FakeTokenSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.post_calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._responses.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _patch_oauth2_network_validator(monkeypatch):
    import providerharvest_service.engine.auth_strategies.oauth2_client_credentials as mod
    monkeypatch.setattr(mod, "assert_safe_http_url", lambda url, allow_private_ranges=False: None)
    yield


def test_oauth2_fetches_token_and_sets_bearer_header():
    session = _FakeTokenSession([_FakeTokenResponse(json_body={"access_token": "tok-1", "expires_in": 300})])
    strategy = OAuth2ClientCredentialsAuth(session_factory=lambda: session)
    kwargs = strategy.apply(
        {},
        SecretBundle(fields={"client_id": "cid", "client_secret": "csecret"}),
        {"token_url": "https://provider.example.com/oauth/token"},
    )
    assert kwargs["headers"] == {"Authorization": "Bearer tok-1"}
    assert session.post_calls[0][0] == "https://provider.example.com/oauth/token"
    assert session.post_calls[0][1]["auth"] == ("cid", "csecret")
    assert session.closed is True


def test_oauth2_caches_token_across_calls():
    session = _FakeTokenSession([_FakeTokenResponse(json_body={"access_token": "tok-1", "expires_in": 300})])
    strategy = OAuth2ClientCredentialsAuth(session_factory=lambda: session)
    secret = SecretBundle(fields={"client_id": "cid", "client_secret": "csecret"})
    auth_opts = {"token_url": "https://provider.example.com/oauth/token"}

    strategy.apply({}, secret, auth_opts)
    strategy.apply({}, secret, auth_opts)

    assert len(session.post_calls) == 1  # second call reused the cached token


def test_oauth2_refetches_after_expiry(monkeypatch):
    import providerharvest_service.engine.auth_strategies.oauth2_client_credentials as mod
    session = _FakeTokenSession([
        _FakeTokenResponse(json_body={"access_token": "tok-1", "expires_in": 300}),
        _FakeTokenResponse(json_body={"access_token": "tok-2", "expires_in": 300}),
    ])
    strategy = OAuth2ClientCredentialsAuth(session_factory=lambda: session)
    secret = SecretBundle(fields={"client_id": "cid", "client_secret": "csecret"})
    auth_opts = {"token_url": "https://provider.example.com/oauth/token"}

    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])

    kwargs1 = strategy.apply({}, secret, auth_opts)
    assert kwargs1["headers"]["Authorization"] == "Bearer tok-1"

    clock["t"] += 1000  # well past expires_in - safety margin
    kwargs2 = strategy.apply({}, secret, auth_opts)
    assert kwargs2["headers"]["Authorization"] == "Bearer tok-2"
    assert len(session.post_calls) == 2


def test_oauth2_requires_token_url():
    strategy = OAuth2ClientCredentialsAuth(session_factory=lambda: _FakeTokenSession([]))
    with pytest.raises(ValueError):
        strategy.apply({}, SecretBundle(fields={"client_id": "a", "client_secret": "b"}), {})


def test_oauth2_requires_client_credentials():
    strategy = OAuth2ClientCredentialsAuth(session_factory=lambda: _FakeTokenSession([]))
    with pytest.raises(ValueError):
        strategy.apply(
            {}, SecretBundle(fields={}), {"token_url": "https://provider.example.com/oauth/token"}
        )


def test_oauth2_missing_access_token_in_response_raises():
    session = _FakeTokenSession([_FakeTokenResponse(json_body={"token_type": "bearer"})])
    strategy = OAuth2ClientCredentialsAuth(session_factory=lambda: session)
    with pytest.raises(ValueError):
        strategy.apply(
            {},
            SecretBundle(fields={"client_id": "cid", "client_secret": "csecret"}),
            {"token_url": "https://provider.example.com/oauth/token"},
        )


# -- MTLSAuth ------------------------------------------------------------

def test_mtls_writes_cert_and_key_to_temp_files():
    strategy = MTLSAuth()
    try:
        kwargs = strategy.apply(
            {},
            SecretBundle(fields={
                "client_cert_pem": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----",
                "client_key_pem": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
            }),
            {},
        )
        cert_path, key_path = kwargs["cert"]
        assert os.path.exists(cert_path)
        assert os.path.exists(key_path)
        with open(cert_path) as fh:
            assert "BEGIN CERTIFICATE" in fh.read()
        with open(key_path) as fh:
            assert "BEGIN PRIVATE KEY" in fh.read()
    finally:
        strategy.close()


def test_mtls_close_removes_temp_files():
    strategy = MTLSAuth()
    kwargs = strategy.apply(
        {},
        SecretBundle(fields={
            "client_cert_pem": "cert-content", "client_key_pem": "key-content",
        }),
        {},
    )
    cert_path, key_path = kwargs["cert"]
    strategy.close()
    assert not os.path.exists(cert_path)
    assert not os.path.exists(key_path)


def test_mtls_reuses_same_temp_files_across_calls():
    strategy = MTLSAuth()
    try:
        secret = SecretBundle(fields={"client_cert_pem": "cert", "client_key_pem": "key"})
        kwargs1 = strategy.apply({}, secret, {})
        kwargs2 = strategy.apply({}, secret, {})
        assert kwargs1["cert"] == kwargs2["cert"]
    finally:
        strategy.close()


def test_mtls_missing_cert_raises():
    with pytest.raises(ValueError):
        MTLSAuth().apply({}, SecretBundle(fields={"client_key_pem": "key"}), {})


def test_mtls_missing_key_raises():
    with pytest.raises(ValueError):
        MTLSAuth().apply({}, SecretBundle(fields={"client_cert_pem": "cert"}), {})


def test_mtls_close_is_a_safe_noop_when_never_applied():
    MTLSAuth().close()  # must not raise
