import base64
import os

import pytest

from providerharvest_service import db
from providerharvest_service.ckan_client import CKANActionClient
from providerharvest_service.ckan_tokens import (
    CkanTokenProvisioningError,
    get_or_provision_ckan_client,
)


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body

    def json(self):
        return self._json_body


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("CKAN_PROVIDERHARVEST_MASTER_KEY", key)


@pytest.fixture(autouse=True)
def _base_url(monkeypatch):
    monkeypatch.setenv("CKAN_BASE_URL", "https://data.gov.gr")


@pytest.fixture
def session(tmp_path):
    engine = db.make_engine("sqlite:///%s" % (tmp_path / "tokens-test.db"))
    return db.make_session_factory(engine)()


def _fake_sysadmin(raw_token="minted-token-abc123"):
    response = FakeResponse(json_body={"success": True, "result": {"token": raw_token}})
    session_obj = FakeSession(response)
    client = CKANActionClient("https://data.gov.gr", "sysadmin-token", session=session_obj)
    return client, session_obj


def test_first_call_mints_a_token_via_the_sysadmin_client(session):
    sysadmin, sysadmin_session = _fake_sysadmin()

    client = get_or_provision_ckan_client(session, "alice", sysadmin_client=sysadmin)

    assert isinstance(client, CKANActionClient)
    assert len(sysadmin_session.calls) == 1
    url, kwargs = sysadmin_session.calls[0]
    assert url.endswith("/api/3/action/api_token_create")
    assert kwargs["json"] == {"user": "alice", "name": "loader-data-gov-gr"}
    # The sysadmin's own token is never used for the returned client --
    # that's the whole point, the caller gets alice's own token.
    assert kwargs["headers"]["Authorization"] == "sysadmin-token"


def test_second_call_reuses_the_stored_token_without_minting_again(session):
    sysadmin, sysadmin_session = _fake_sysadmin(raw_token="alices-real-token")

    get_or_provision_ckan_client(session, "alice", sysadmin_client=sysadmin)
    assert len(sysadmin_session.calls) == 1

    # A second provisioning call for the SAME user must not mint again --
    # pass a sysadmin client whose only response would be wrong if it
    # were actually called, to prove it wasn't.
    poison_response = FakeResponse(json_body={"success": True, "result": {"token": "WRONG"}})
    poison_session = FakeSession(poison_response)
    poison_sysadmin = CKANActionClient("https://data.gov.gr", "sysadmin-token", session=poison_session)

    get_or_provision_ckan_client(session, "alice", sysadmin_client=poison_sysadmin)

    assert len(poison_session.calls) == 0


def test_returned_client_actually_carries_the_provisioned_token(session):
    sysadmin, _ = _fake_sysadmin(raw_token="alices-real-token")
    client = get_or_provision_ckan_client(session, "alice", sysadmin_client=sysadmin)

    # Exercise the returned client for real -- its own Authorization
    # header should carry alice's token, not the sysadmin's.
    own_response = FakeResponse(json_body={"success": True, "result": {"ok": True}})
    own_session = FakeSession(own_response)
    client._session = own_session  # swap in a fake transport for this one call

    client.call("package_list", {})

    _, kwargs = own_session.calls[0]
    assert kwargs["headers"]["Authorization"] == "alices-real-token"


def test_different_users_get_different_tokens(session):
    alice_sysadmin, _ = _fake_sysadmin(raw_token="alices-token")
    bob_sysadmin, _ = _fake_sysadmin(raw_token="bobs-token")

    alice_client = get_or_provision_ckan_client(session, "alice", sysadmin_client=alice_sysadmin)
    bob_client = get_or_provision_ckan_client(session, "bob", sysadmin_client=bob_sysadmin)

    assert alice_client._token == "alices-token"
    assert bob_client._token == "bobs-token"


def test_missing_ckan_base_url_raises_clearly(session, monkeypatch):
    monkeypatch.delenv("CKAN_BASE_URL", raising=False)
    with pytest.raises(CkanTokenProvisioningError):
        get_or_provision_ckan_client(session, "alice")


def test_missing_sysadmin_token_raises_clearly_when_minting_is_needed(session, monkeypatch):
    monkeypatch.delenv("CKAN_SYSADMIN_API_TOKEN", raising=False)
    with pytest.raises(CkanTokenProvisioningError):
        # No sysadmin_client injected, and no stored token yet for this
        # user -- must fall through to _sysadmin_client(), which needs
        # the env var.
        get_or_provision_ckan_client(session, "someone-never-provisioned")


def test_stored_token_is_actually_encrypted_at_rest(session):
    from providerharvest_service.models import CkanUserToken

    sysadmin, _ = _fake_sysadmin(raw_token="super-secret-token-value")
    get_or_provision_ckan_client(session, "alice", sysadmin_client=sysadmin)

    row = session.get(CkanUserToken, "alice")
    assert row is not None
    assert b"super-secret-token-value" not in row.ciphertext
