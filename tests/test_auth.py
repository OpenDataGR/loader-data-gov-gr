import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from providerharvest_service import auth
from providerharvest_service.auth import decode_bearer_token, require_current_username

ISSUER = "https://auth.data.gov.gr/realms/data"
AUDIENCE = "providerharvest-api"
KID = "test-key-1"


@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture
def resolver(keypair):
    _, public_pem = keypair

    def _resolve(kid: str) -> str:
        assert kid == KID
        return public_pem

    return _resolve


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ISSUER", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)


@pytest.fixture(autouse=True)
def _use_test_resolver(monkeypatch, resolver):
    # require_current_username (the real FastAPI dependency, no
    # resolver-injection parameter by design -- production always uses
    # the real JWKS-backed one) goes through the module-level default;
    # swap that out here so it's the actual function under test, not a
    # hand-rolled re-implementation of its logic.
    monkeypatch.setattr(auth, "_default_key_resolver", resolver)


def _make_token(keypair, *, username="alice", issuer=ISSUER, audience=AUDIENCE,
                 expires_in=300, kid=KID, include_username=True, extra_claims=None):
    private_pem, _ = keypair
    claims = {
        "iss": issuer,
        "aud": audience,
        "exp": int(time.time()) + expires_in,
        "iat": int(time.time()),
    }
    if include_username:
        claims["preferred_username"] = username
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


def test_valid_token_decodes_to_the_right_claims(keypair, resolver):
    token = _make_token(keypair, username="alice")

    claims = decode_bearer_token(
        "Bearer %s" % token, key_resolver=resolver, issuer=ISSUER, audience=AUDIENCE
    )

    assert claims["preferred_username"] == "alice"


def test_require_current_username_returns_the_username(keypair):
    token = _make_token(keypair, username="bob")

    username = require_current_username("Bearer %s" % token)

    assert username == "bob"


def test_require_current_username_rejects_missing_claim(keypair):
    token = _make_token(keypair, include_username=False)

    with pytest.raises(HTTPException) as excinfo:
        require_current_username("Bearer %s" % token)
    assert excinfo.value.status_code == 401


def test_missing_authorization_header_is_401():
    with pytest.raises(HTTPException) as excinfo:
        decode_bearer_token("", key_resolver=lambda kid: "unused", issuer=ISSUER, audience=AUDIENCE)
    assert excinfo.value.status_code == 401


def test_non_bearer_header_is_401():
    with pytest.raises(HTTPException) as excinfo:
        decode_bearer_token(
            "Basic dXNlcjpwYXNz", key_resolver=lambda kid: "unused",
            issuer=ISSUER, audience=AUDIENCE,
        )
    assert excinfo.value.status_code == 401


def test_expired_token_is_401(keypair, resolver):
    token = _make_token(keypair, expires_in=-60)

    with pytest.raises(HTTPException) as excinfo:
        decode_bearer_token("Bearer %s" % token, key_resolver=resolver, issuer=ISSUER, audience=AUDIENCE)
    assert excinfo.value.status_code == 401


def test_wrong_issuer_is_401(keypair, resolver):
    token = _make_token(keypair, issuer="https://not-the-real-realm.example.com")

    with pytest.raises(HTTPException) as excinfo:
        decode_bearer_token("Bearer %s" % token, key_resolver=resolver, issuer=ISSUER, audience=AUDIENCE)
    assert excinfo.value.status_code == 401


def test_wrong_audience_is_401(keypair, resolver):
    token = _make_token(keypair, audience="some-other-client")

    with pytest.raises(HTTPException) as excinfo:
        decode_bearer_token("Bearer %s" % token, key_resolver=resolver, issuer=ISSUER, audience=AUDIENCE)
    assert excinfo.value.status_code == 401


def test_tampered_signature_is_401(keypair, resolver):
    token = _make_token(keypair)
    # Flip a character in the signature segment -- the token must still
    # parse structurally (three dot-separated segments) but fail
    # cryptographic verification.
    header, payload, signature = token.split(".")
    tampered_char = "A" if signature[-1] != "A" else "B"
    tampered = "%s.%s.%s%s" % (header, payload, signature[:-1], tampered_char)

    with pytest.raises(HTTPException) as excinfo:
        decode_bearer_token(
            "Bearer %s" % tampered, key_resolver=resolver, issuer=ISSUER, audience=AUDIENCE
        )
    assert excinfo.value.status_code == 401


def test_token_signed_by_a_different_key_is_401(resolver):
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    claims = {
        "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300,
        "preferred_username": "eve",
    }
    forged = jwt.encode(claims, other_pem, algorithm="RS256", headers={"kid": KID})

    with pytest.raises(HTTPException) as excinfo:
        decode_bearer_token("Bearer %s" % forged, key_resolver=resolver, issuer=ISSUER, audience=AUDIENCE)
    assert excinfo.value.status_code == 401


def test_token_with_no_kid_header_is_401(keypair):
    private_pem, _ = keypair
    claims = {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300, "preferred_username": "alice"}
    token = jwt.encode(claims, private_pem, algorithm="RS256")  # no kid header

    with pytest.raises(HTTPException) as excinfo:
        decode_bearer_token(
            "Bearer %s" % token, key_resolver=lambda kid: "unused", issuer=ISSUER, audience=AUDIENCE
        )
    assert excinfo.value.status_code == 401
