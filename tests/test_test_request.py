"""The test request, set on the source: validated when saved, shown again when editing, and used by "Test connection" -- one real call
with it (the strongest check) instead of the harmless GET."""

import json
import re

import pytest

from providerharvest_service import connection_check, provider_sources, proxy
from tests.test_proxy import FakeUpstreamResponse  # noqa: F401
from tests.test_stored_secret_hint import (  # noqa: F401  (fixtures and helpers shared with the provider UI tests)
    _admin_session_env, _isolated_db, _isolated_harvest_db, _master_key, _valid_form, client,
)

BODY = '{"document": {"statements": {"platenumber": "TEST123"}}}'


def _form(**extra):
    return {"name": "registry", "title": "Registry", "transport_type": "http", "endpoint_url": "https://registry.example.gr/api/",
            "auth_type": "api_key", "api_key": "abc", "api_key_location": "header", "api_key_name": "X-API-Key", **extra}


# -- saving it --------------------------------------------------------------------------------------------------------------------

def test_a_post_sources_test_request_must_be_json_and_is_kept(_isolated_db):
    with _isolated_db() as session:
        source = provider_sources.register(session, _form(proxy_method="POST", test_request="  %s  " % BODY))
        assert source.test_request == BODY
        for bad in ("not json", "{oops", "x" * 70000):
            with pytest.raises(provider_sources.ValidationError) as excinfo:
                provider_sources.register(session, _form(name="other", proxy_method="POST", test_request=bad))
            assert "test_request" in excinfo.value.errors


def test_a_get_sources_test_request_is_a_one_line_query_string(_isolated_db):
    with _isolated_db() as session:
        source = provider_sources.register(session, _form(test_request="?plate=AB1234&kind=car"))
        assert source.test_request == "?plate=AB1234&kind=car" and source.proxy_method == "GET"
        for bad in ("a=1\\nb=2".replace("\\n", "\n"), "x" * 2100):
            with pytest.raises(provider_sources.ValidationError) as excinfo:
                provider_sources.register(session, _form(name="other", test_request=bad))
            assert "test_request" in excinfo.value.errors


def test_an_empty_test_request_is_none_and_clearing_it_on_edit_removes_it(_isolated_db):
    with _isolated_db() as session:
        source = provider_sources.register(session, _form(proxy_method="POST", test_request=BODY))
        source_id = source.id
        assert provider_sources.register(session, _form(name="plain")).test_request == ""
    with _isolated_db() as session:
        provider_sources.update(session, source_id, _form(proxy_method="POST", test_request="", api_key=""))
        assert provider_sources.get_or_404(session, source_id).test_request == ""


def test_the_form_has_the_field_shows_it_again_on_edit_and_explains_both_methods(client):
    new_page = client.get("/provider/sources/new").text
    assert 'name="test_request"' in new_page and "Test request (optional)" in new_page and "one real POST" in new_page and "one real GET" in new_page
    client.post("/provider/sources/new", data=_valid_form(name="postsrc", proxy_method="POST", test_request=BODY))
    source_id = re.search(r"/(?:provider|admin)/sources/([^/]+)/check-connection", client.get("/provider/sources").text).group(1)
    page = client.get("/provider/sources/%s/edit" % source_id).text
    assert re.search(r'<textarea[^>]*id="test_request"[^>]*>[^<]*TEST123', page)
    assert 'errors' not in page


def test_a_bad_test_request_reshows_the_form_with_the_reason(client):
    resp = client.post("/provider/sources/new", data=_valid_form(name="badreq", proxy_method="POST", test_request="not json"))
    assert resp.status_code == 400 and "must be valid JSON" in resp.text  # how every form error is shown: the form again, with the reason


# -- "Test connection" -------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def calls(monkeypatch):
    state = {"get": [], "post": [], "response": FakeUpstreamResponse(content=b'{"ok": true}'), "error": None}

    def make(kind):
        def fake(url, **kw):
            state[kind].append((url, kw))
            if state["error"]:
                raise state["error"]
            return state["response"]
        return fake

    monkeypatch.setattr(proxy.requests, "post", make("post"))
    monkeypatch.setattr(proxy.requests, "get", make("get"))
    monkeypatch.setattr(connection_check.requests, "get", make("get"))
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)
    monkeypatch.setattr(connection_check, "assert_safe_http_url", lambda url: None)
    return state


def _check(_isolated_db, **form):
    with _isolated_db() as session:
        source = provider_sources.register(session, _form(**form))
        source_id = source.id
    with _isolated_db() as session:
        checked = connection_check.check_connection(session, source_id)
        return checked.last_check_status, checked.last_check_message


def test_with_a_test_request_a_post_api_gets_that_real_post_and_the_answer_decides(_isolated_db, calls):
    status, message = _check(_isolated_db, proxy_method="POST", test_request=BODY)
    assert status == "ok" and "HTTP 200" in message and "real POST" in message and "may have created a record" in message
    (url, kw), = calls["post"]
    assert kw["data"] == BODY.encode() and kw["headers"]["X-API-Key"] == "abc" and calls["get"] == []  # a POST, not the harmless GET


@pytest.mark.parametrize("code,fragment", [(401, "refused the credential"), (403, "refused the credential"), (404, "404 (not found)"), (400, "refused the test request"),
                                          (422, "refused the test request"), (500, "server error"), (503, "HTTP 503")])
def test_a_refusal_is_explained_without_repeating_what_the_provider_said(_isolated_db, calls, code, fragment):
    calls["response"] = FakeUpstreamResponse(status_code=code, content=b'{"secret-detail": "do not repeat"}')
    status, message = _check(_isolated_db, proxy_method="POST", test_request=BODY)
    assert status == "error" and fragment in message and "do not repeat" not in message


def test_an_unreachable_provider_is_an_error_with_the_reason(_isolated_db, calls):
    calls["error"] = proxy.requests.ConnectionError("name resolution failed")
    status, message = _check(_isolated_db, proxy_method="POST", test_request=BODY)
    assert status == "error" and "Not reachable" in message and "name resolution" in message


def test_a_get_api_with_a_test_request_gets_one_real_get_with_those_parameters(_isolated_db, calls):
    status, message = _check(_isolated_db, test_request="plate=AB1234&kind=car")
    assert status == "ok" and "real GET" in message
    (url, kw), = calls["get"]
    assert kw["params"] == {"plate": "AB1234", "kind": "car"} and calls["post"] == []


def test_without_a_test_request_a_post_api_still_gets_only_the_harmless_get(_isolated_db, calls):
    calls["response"] = FakeUpstreamResponse(status_code=405)
    status, message = _check(_isolated_db, proxy_method="POST")
    assert status == "ok" and "harmless GET" in message and calls["post"] == []


def test_the_test_connection_button_asks_first_only_when_it_will_make_a_real_post(client):
    client.post("/provider/sources/new", data=_valid_form(name="withtest", proxy_method="POST", test_request=BODY))
    client.post("/provider/sources/new", data=_valid_form(name="withouttest", proxy_method="POST"))
    client.post("/provider/sources/new", data=_valid_form(name="ordinary"))
    page = client.get("/provider/sources").text
    forms = re.findall(r'<form method="post" action="[^"]*/check-connection"[^>]*>', page)
    assert len(forms) == 3
    asking = [f for f in forms if "confirm(" in f]
    assert len(asking) == 1 and "may create a record" in asking[0]
