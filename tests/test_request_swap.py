"""The live proxy's request side: callers use the other method than the provider (GET -> POST and POST -> GET), a template turns their
call into the provider's, and a caller's value can never change the structure of what is sent."""

import json

import pytest

from providerharvest_service import proxy, request_template as rt
from tests.test_live_transform import (  # noqa: F401  (fixtures and helpers shared with the live-transform tests)
    ANSWER, JSON_BODY, _build, _caller, _flash, _http_source, _isolated_db, _isolated_harvest_db, _master_key, _registry, answer, client,
    source, FakeUpstreamResponse, _clear_rate_limiters,
)
from tests.test_load_box import _row

REGISTRY_BODY = ('{"document": {"statements": {"platenumber": "{plate}", "plate_number": "{plate}"}, "attachments": {}, '
                 '"template": {"refname": "VEHICLE-INSURANCE-DATAGOV-DEMO", "digest-sha256": ""}}}')


# -- the templates ----------------------------------------------------------------------------------------------------------------

def test_a_json_body_is_built_from_parameters_and_values_stay_strings_inside_it():
    body = json.loads(rt.build_json_body(REGISTRY_BODY, {"plate": "ZMM3707"}))
    assert body["document"]["statements"] == {"platenumber": "ZMM3707", "plate_number": "ZMM3707"}
    assert body["document"]["template"]["refname"] == "VEHICLE-INSURANCE-DATAGOV-DEMO"  # the rest of the template is as written


@pytest.mark.parametrize("value", ['X","evil":"1', 'a"}}, "x": {"b": "c', "back\\slash", 'quote"', "Πατρα ΠΡΑ", "{plate}", "{}"])
def test_a_callers_value_can_never_change_the_structure_of_the_body(value):
    body = json.loads(rt.build_json_body(REGISTRY_BODY, {"plate": value}))
    assert body["document"]["statements"]["platenumber"] == value and list(body["document"]) == ["statements", "attachments", "template"]


@pytest.mark.parametrize("params,message", [
    ({}, "'plate' is missing"), ({"plate": "x" * 201}, "longer than"), ({"plate": "a\nb"}, "control character"), ({"plate": "a\x00b"}, "control character"),
    ({"plate": ["a"]}, "text or a number"), ({"plate": None}, "text or a number"), ({"plate": True}, "text or a number"),
])
def test_missing_long_or_odd_values_are_refused_with_the_name_not_the_value(params, message):
    with pytest.raises(rt.RequestTemplateError, match=message) as excinfo:
        rt.build_json_body(REGISTRY_BODY, params)
    assert "x" * 50 not in str(excinfo.value)


@pytest.mark.parametrize("template,message", [
    ("", "Write the JSON body"), ('{"a": "b"}', "no {name} placeholder"), ('{"a": {plate}}', "inside quotes"), ('{"a": "{plate}"', "not valid JSON"),
    ('"{plate}"', "object or list"), ('{"a": "%s"}' % ("{p}" * 2000), "larger than"),
])
def test_a_bad_json_template_is_refused_when_it_is_saved(template, message):
    with pytest.raises(rt.RequestTemplateError, match=message):
        rt.validate_json_template(template)


def test_query_parameters_are_taken_from_named_fields_of_the_body():
    assert rt.build_query("plate={plate}&kind={vehicle/type}&fixed=yes", {"plate": "AB1234", "vehicle": {"type": "car"}, "ignored": "x"}) == {
        "plate": "AB1234", "kind": "car", "fixed": "yes"}
    assert rt.build_query("plate={plate}\nq={plate} {plate}", {"plate": "AB"}) == {"plate": "AB", "q": "AB AB"}


@pytest.mark.parametrize("body,message", [({}, "'plate' is missing"), ({"plate": {"a": 1}}, "text or a number"), ({"plate": "x" * 300}, "longer than"), ([1], "JSON object")])
def test_a_body_that_does_not_fit_the_query_template_is_refused(body, message):
    with pytest.raises(rt.RequestTemplateError, match=message):
        rt.build_query("plate={plate}", body)


@pytest.mark.parametrize("template,message", [("", "Write the query"), ("plate", "not a name=value"), ("pl ate={x}", "not a name=value"), ("&&", "no name=value")])
def test_a_bad_query_template_is_refused_when_it_is_saved(template, message):
    with pytest.raises(rt.RequestTemplateError, match=message):
        rt.validate_query_template(template)


# -- GET from callers, POST to the provider ------------------------------------------------------------------------------------------------

def _swap(client, source_id, template=REGISTRY_BODY, mode="swap"):
    return client.post("/transform/sources/%s/request" % source_id, data={"mode": mode, "template": template}, follow_redirects=False)


def test_callers_use_a_plain_link_and_the_provider_gets_the_post_it_expects(client, source, _isolated_db, answer):
    registry_id = _registry(_isolated_db)
    client.post("/transform/sources/%s/create" % registry_id, follow_redirects=False)
    assert "Callers now use GET on /data/registry" in _flash(_swap(client, registry_id))
    resp = _caller().get("/proxy/registry", params={"plate": "ZMM3707", "ignored": "x"})
    assert resp.status_code == 200 and resp.json() == ANSWER  # no response transform here: the answer passes through
    assert answer["calls"] == 1


def test_the_provider_receives_exactly_the_built_body_and_the_loaders_credential_only(client, source, _isolated_db, monkeypatch):
    registry_id = _registry(_isolated_db)
    client.post("/transform/sources/%s/create" % registry_id, follow_redirects=False)
    _swap(client, registry_id)
    sent = []
    monkeypatch.setattr(proxy.requests, "post", lambda url, **kw: (sent.append(kw), FakeUpstreamResponse(content=b"{}"))[-1])
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)
    _caller().get("/proxy/registry", params={"plate": 'X","evil":"1', "other": "dropped"}, headers={"Authorization": "Token stolen"})
    kw = sent[0]
    assert json.loads(kw["data"])["document"]["statements"]["platenumber"] == 'X","evil":"1'
    assert kw["headers"]["Content-Type"] == "application/json" and kw["headers"]["X-API-Key"] == "k" and "Authorization" not in kw["headers"]
    assert kw["params"] == {}  # the caller's query parameters are not forwarded as query parameters
    assert "other" not in kw["data"].decode()


def test_the_post_method_is_not_accepted_any_more_and_a_missing_parameter_is_a_400(client, source, _isolated_db, answer):
    registry_id = _registry(_isolated_db)
    client.post("/transform/sources/%s/create" % registry_id, follow_redirects=False)
    _swap(client, registry_id)
    resp = _caller().post("/proxy/registry", content="{}", headers=JSON_BODY)
    assert resp.status_code == 405 and resp.headers["allow"] == "GET"
    missing = _caller().get("/proxy/registry")
    assert missing.status_code == 400 and "'plate' is missing" in missing.text
    assert _caller().get("/proxy/registry", params={"plate": "x" * 300}).status_code == 400
    assert answer["calls"] == 0  # nothing reached the provider


def test_turned_around_and_reshaped_the_caller_gets_one_small_object_from_a_link(client, source, _isolated_db, answer):
    registry_id = _registry(_isolated_db)
    _build(client, registry_id)  # the live-transform setup: keeps five fields, one object
    _swap(client, registry_id)
    resp = _caller().get("/proxy/registry", params={"plate": "ZMM3707"})
    assert resp.status_code == 200 and set(resp.json()) == {"plate_number", "make", "emporiki_onomasia_ochimatos", "colour", "kylindrismos"}


def test_turning_it_off_returns_to_the_providers_own_method(client, source, _isolated_db, answer):
    registry_id = _registry(_isolated_db)
    client.post("/transform/sources/%s/create" % registry_id, follow_redirects=False)
    _swap(client, registry_id)
    assert "same method as the provider (POST)" in _flash(_swap(client, registry_id, mode="same"))
    assert _caller().get("/proxy/registry", params={"plate": "x"}).status_code == 405
    assert _caller().post("/proxy/registry", content="{}", headers=JSON_BODY).status_code == 200


def test_a_bad_template_is_refused_and_the_setting_is_left_alone(client, source, _isolated_db, answer):
    registry_id = _registry(_isolated_db)
    client.post("/transform/sources/%s/create" % registry_id, follow_redirects=False)
    for bad in ("", '{"a": {plate}}', '{"a": "b"}'):
        assert "Callers now use" not in _flash(_swap(client, registry_id, template=bad))
    assert _caller().get("/proxy/registry", params={"plate": "x"}).status_code == 405  # still the provider's own method
    assert "No such transform" in _flash(client.post("/transform/sources/nope/request", data={"mode": "swap", "template": REGISTRY_BODY}, follow_redirects=False))


# -- POST from callers, GET to the provider ---------------------------------------------------------------------------------------------------

@pytest.fixture
def get_provider(client, source, _isolated_db, monkeypatch):
    source_id = _http_source(_isolated_db, "lookup")
    client.post("/transform/sources/%s/create" % source_id, follow_redirects=False)
    calls = []
    monkeypatch.setattr(proxy.requests, "get", lambda url, **kw: (calls.append((url, kw)), FakeUpstreamResponse(content=b'{"found": true}'))[-1])
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)
    return {"id": source_id, "calls": calls}


def test_callers_post_a_json_body_and_the_provider_gets_a_get_with_query_parameters(client, get_provider):
    assert "Callers now use POST on /data/lookup" in _flash(_swap(client, get_provider["id"], template="plate={plate}&kind={vehicle/type}"))
    resp = _caller().post("/proxy/lookup", content=json.dumps({"plate": "AB1234", "vehicle": {"type": "car"}, "other": "dropped"}), headers=JSON_BODY)
    assert resp.status_code == 200 and resp.json() == {"found": True}
    url, kw = get_provider["calls"][0]
    assert kw["params"]["plate"] == "AB1234" and kw["params"]["kind"] == "car" and "other" not in kw["params"]
    assert "data" not in kw  # a GET carries no body


def test_the_get_side_refuses_a_get_and_a_body_that_does_not_fit(client, get_provider):
    _swap(client, get_provider["id"], template="plate={plate}")
    refused = _caller().get("/proxy/lookup", params={"plate": "AB"})
    assert refused.status_code == 405 and refused.headers["allow"] == "POST"
    assert _caller().post("/proxy/lookup", content="{}", headers=JSON_BODY).status_code == 400  # the field is missing
    assert _caller().post("/proxy/lookup", content="[1]", headers=JSON_BODY).status_code == 400  # not an object
    assert _caller().post("/proxy/lookup", content='{"plate": {"a": 1}}', headers=JSON_BODY).status_code == 400
    assert _caller().post("/proxy/lookup", content="not json", headers=JSON_BODY).status_code == 400
    assert get_provider["calls"] == []


def test_the_providers_own_credential_is_added_and_a_caller_cannot_override_it(client, source, _isolated_db, monkeypatch):
    source_id = _http_source(_isolated_db, "keyed", api_key_location="query", api_key_name="api_key")
    client.post("/transform/sources/%s/create" % source_id, follow_redirects=False)
    _swap(client, source_id, template="plate={plate}&api_key={apikey}")
    seen = []
    monkeypatch.setattr(proxy.requests, "get", lambda url, **kw: (seen.append(kw), FakeUpstreamResponse())[-1])
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)
    _caller().post("/proxy/keyed", content='{"plate": "AB", "apikey": "stolen"}', headers=JSON_BODY)
    assert seen[0]["params"]["api_key"] == "k"  # the loader's key wins over whatever a template or caller put there


# -- what the pages say ----------------------------------------------------------------------------------------------------------------------------

def test_the_load_box_shows_the_call_as_callers_make_it_and_the_transform_page_the_setting(client, source, _isolated_db, answer):
    registry_id = _registry(_isolated_db)
    client.post("/transform/sources/%s/create" % registry_id, follow_redirects=False)
    before = _row(client.get("/load").text, "registry")
    assert "POST" in before and "?plate" not in before
    _swap(client, registry_id)
    row = _row(client.get("/load").text, "registry")
    assert "GET" in row and "/data/registry?plate=..." in row and "sent to the provider as POST" in row
    assert 'href="https://testserver/data/registry' not in row  # not offered as a link: the address needs its parameter
    page = client.get("/transform/sources/%s" % registry_id).text
    assert "Request: how callers ask" in page and "GET &rarr; POST" in page and "plate" in page


def test_an_ordinary_source_is_unchanged_by_all_this(client, source, _isolated_db, monkeypatch):
    source_id = _http_source(_isolated_db, "plain")
    seen = []
    monkeypatch.setattr(proxy.requests, "get", lambda url, **kw: (seen.append(kw), FakeUpstreamResponse())[-1])
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)
    assert _caller().get("/proxy/plain", params={"category": "x"}).status_code == 200
    assert seen[0]["params"]["category"] == "x"  # parameters still pass through, as they always did
