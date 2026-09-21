import json

import pytest

from providerharvest_service.engine.auth_strategies.api_key import ApiKeyAuth
from providerharvest_service.engine.validators import UnsafeNetworkTargetError
from providerharvest_service.engine.secrets.base import SecretBundle
from providerharvest_service.engine.transport.direct_https import DirectHTTPSTransport


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body or {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.verify = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._responses.pop(0)

    def close(self):
        pass


def _public_resolver(hostname, port):
    return [(None, None, None, None, ("203.0.113.10", 0))]


def _transport(responses, **overrides):
    session = FakeSession(responses)
    events = []
    kwargs = dict(
        base_url="https://provider.example.com/api/records",
        auth_strategy=ApiKeyAuth(),
        secret=SecretBundle(fields={"api_key": "k"}),
        pagination={"style": "page_number", "items_path": "results"},
        session_factory=lambda: session,
        on_request=events.append,
        harvest_source_id="src-1",
        harvest_job_id="job-1",
    )
    kwargs.update(overrides)
    transport = DirectHTTPSTransport(**kwargs)
    # patch the validator's resolver indirectly by monkeypatching socket at
    # module import time would be heavier than needed -- instead exercise
    # assert_safe_http_url directly in its own tests, and here just avoid
    # touching real DNS by using a loopback-safe base_url override path:
    return transport, session, events


@pytest.fixture(autouse=True)
def patch_resolver(monkeypatch):
    import providerharvest_service.engine.transport.direct_https as mod
    monkeypatch.setattr(
        mod, "assert_safe_http_url", lambda url, allow_private_ranges=False: ["203.0.113.10"]
    )
    # Retry/backoff sleeps use real wall-clock time by design in
    # production; stub it out here so retry tests stay fast.
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)
    yield


def test_single_page_stops_when_empty():
    responses = [FakeResponse(json_body={"results": []})]
    transport, session, events = _transport(responses)
    with transport:
        page = transport.list_entries(cursor=None)
    assert page.entries == []
    assert page.next_cursor is None


def test_extracts_records_and_advances_page_number():
    responses = [FakeResponse(json_body={"results": [{"id": 1}, {"id": 2}]})]
    transport, session, events = _transport(
        responses, pagination={"style": "page_number", "items_path": "results", "guid_field": "id"}
    )
    with transport:
        page = transport.list_entries(cursor=None)

    assert [e.ref for e in page.entries] == ["1", "2"]
    assert page.next_cursor == "2"  # page 1 had records -> next page is 2
    assert json.loads(page.entries[0].inline_data) == {"id": 1}


def test_cursor_style_pagination():
    responses = [FakeResponse(json_body={"results": [{"id": 1}], "next": "abc"})]
    transport, session, events = _transport(
        responses, pagination={"style": "cursor", "items_path": "results",
                                "next_cursor_path": "next", "guid_field": "id"}
    )
    with transport:
        page = transport.list_entries(cursor=None)
    assert page.next_cursor == "abc"


def test_open_entry_returns_inline_data():
    responses = [FakeResponse(json_body={"results": [{"id": 1}]})]
    transport, session, events = _transport(
        responses, pagination={"style": "page_number", "items_path": "results", "guid_field": "id"}
    )
    with transport:
        page = transport.list_entries(cursor=None)
        body = transport.open_entry(page.entries[0]).read()
    assert json.loads(body) == {"id": 1}


def test_retries_on_429_then_succeeds():
    responses = [
        FakeResponse(status_code=429),
        FakeResponse(json_body={"results": []}),
    ]
    transport, session, events = _transport(responses)
    with transport:
        transport.list_entries(cursor=None)
    assert len(session.calls) == 2
    assert [e.status for e in events] == [429, 200]


def test_audit_events_never_contain_headers_or_body():
    responses = [FakeResponse(json_body={"results": []})]
    transport, session, events = _transport(responses)
    with transport:
        transport.list_entries(cursor=None)
    event = events[0]
    assert not hasattr(event, "headers")
    assert not hasattr(event, "body")
    assert event.host == "provider.example.com"
    assert event.auth_type_used == "api_key"


def test_custom_ca_bundle_sets_session_verify():
    responses = [FakeResponse(json_body={"results": []})]
    transport, session, events = _transport(responses, ca_bundle_path="/etc/ssl/custom-ca.pem")
    with transport:
        pass
    assert session.verify == "/etc/ssl/custom-ca.pem"


def test_default_verify_is_true_not_disabled():
    responses = [FakeResponse(json_body={"results": []})]
    transport, session, events = _transport(responses)
    with transport:
        pass
    assert session.verify is True


def test_connect_reraises_unsafe_target(monkeypatch):
    import providerharvest_service.engine.transport.direct_https as mod

    def boom(url, allow_private_ranges=False):
        raise UnsafeNetworkTargetError("blocked")

    monkeypatch.setattr(mod, "assert_safe_http_url", boom)
    transport, session, events = _transport([FakeResponse()])
    with pytest.raises(UnsafeNetworkTargetError):
        transport.connect()
