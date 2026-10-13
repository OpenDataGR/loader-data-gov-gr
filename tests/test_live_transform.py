"""A transform on a live answer: the proxy reshapes the provider's answer (keep only some fields, one object back) before it leaves
the loader, on a sample the admin pastes because nothing is stored. Shaped like the vehicle registry's answer."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from providerharvest_service import proxy, transform_engine as te
from providerharvest_service.app import app
from tests.test_load_box import _http_source, _row  # noqa: F401
from tests.test_proxy import FakeUpstreamResponse, _clear_rate_limiters  # noqa: F401
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _flash, _go, _isolated_db, _isolated_harvest_db, _master_key, _steps, client, source,
)

ANSWER = {
    "data": {"document": {"statements": {
        "plate_number": "ZMM3707", "make": "FIAT AUTO SPA PUNTO", "emporiki_onomasia_ochimatos": "PUNTO", "colour": "ΑΣΗΜΙ",
        "kylindrismos": "1368", "arithmos_plaisiou": "ZFA18800000629998", "endeixi_chiliometriti": "123477", "is_insured": "Ναι",
        "imerominia_protis_adeias": "23/10/03",
    }, "case_id": "668507973"}},
    "meta": {},
}
SAMPLE = json.dumps(ANSWER)
PUBLIC_FIELDS = ["plate_number", "make", "emporiki_onomasia_ochimatos", "colour", "kylindrismos"]
READER = {"kind": "read", "format": "json_records", "record": "data/document/statements", "path": PUBLIC_FIELDS,
          **{"name::" + f: f for f in PUBLIC_FIELDS}}
JSON_BODY = {"Content-Type": "application/json"}


def _registry(_isolated_db):
    return _http_source(_isolated_db, "registry", post=True)


def _build(client, registry_id, *, enable=True, sample=SAMPLE):
    client.post("/transform/sources/%s/create" % registry_id, data={"sample": sample}, follow_redirects=False)
    resp = client.post("/transform/sources/%s/steps/new" % registry_id, data=READER, follow_redirects=False)
    assert resp.status_code == 303, resp.text[:300]
    if enable:
        client.post("/transform/sources/%s/enabled" % registry_id, data={"enabled": "1"}, follow_redirects=False)


def _caller():
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def answer(monkeypatch):
    state = {"resp": FakeUpstreamResponse(content=json.dumps(ANSWER).encode()), "calls": 0}

    def post(url, **kw):
        state["calls"] += 1
        return state["resp"]

    monkeypatch.setattr(proxy.requests, "post", post)
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)
    return state


def test_a_transform_is_created_with_a_pasted_sample_that_is_validated(client, source, _isolated_db):
    registry_id = _registry(_isolated_db)
    bad = client.post("/transform/sources/%s/create" % registry_id, data={"sample": "not json {"}, follow_redirects=False)
    assert "not valid JSON" in _flash(bad)
    assert "no transform yet" in client.get("/transform/sources/%s" % registry_id).text or "This source has no transform yet" in client.get("/transform/sources/%s" % registry_id).text
    ok = client.post("/transform/sources/%s/create" % registry_id, data={"sample": SAMPLE}, follow_redirects=False)
    assert "Transform created" in _flash(ok) and "read the answer" in _flash(ok)
    main = client.get("/transform/sources/%s" % registry_id).text
    assert ">IN<" in main and "sample answer (" in main and "kB kept" in main  # the Input step says a sample is kept
    page = client.get("/transform/sources/%s/steps/input" % registry_id).text  # and its own page holds the sample
    assert "one sample answer" in page and "kept," in page and "ZMM3707" in page


def test_the_sample_can_be_replaced_removed_and_is_checked(client, source, _isolated_db):
    registry_id = _registry(_isolated_db)
    _build(client, registry_id, enable=False)
    url = "/transform/sources/%s/sample" % registry_id
    assert "not valid JSON" in _flash(client.post(url, data={"sample": "{oops"}, follow_redirects=False))
    assert "larger than" in _flash(client.post(url, data={"sample": '{"x": "%s"}' % ("a" * 210000)}, follow_redirects=False))
    assert "saved" in _flash(client.post(url, data={"sample": '{"data": {"document": {"statements": {"make": "SEAT"}}}}'}, follow_redirects=False))
    assert "SEAT" in client.get("/transform/sources/%s/steps/input" % registry_id).text
    assert "removed" in _flash(client.post(url, data={"sample": ""}, follow_redirects=False))
    assert "none yet" in client.get("/transform/sources/%s/steps/input" % registry_id).text
    assert "No sample answer yet" in client.get("/transform/sources/%s" % registry_id).text
    assert "No such transform" in _flash(client.post("/transform/sources/nope/sample", data={"sample": "{}"}, follow_redirects=False))


def test_the_steps_are_built_and_tried_on_the_sample_and_show_only_what_is_kept(client, source, _isolated_db):
    registry_id = _registry(_isolated_db)
    _build(client, registry_id, enable=False)
    chooser = client.get("/transform/sources/%s/steps/new?type=read" % registry_id).text
    assert "pasted sample.json" in chooser or "data/document/statements" in chooser
    page = client.post("/transform/sources/%s/preview" % registry_id).text
    shown = page[page.index('id="preview-body"'):]  # the result window (the sample card, which shows the whole sample, is above it)
    assert "FIAT AUTO SPA PUNTO" in shown and "1</strong> record read" in shown  # singular for one record
    assert "pasted sample</strong>" in shown and "received" not in shown.split("</p>")[0]  # a pasted sample was not "received"
    assert "ZFA18800000629998" not in shown and "123477" not in shown  # the fields not kept are not in the result


def test_the_proxy_answers_with_only_the_kept_fields_as_one_object(client, source, _isolated_db, answer):
    registry_id = _registry(_isolated_db)
    _build(client, registry_id)
    resp = _caller().post("/proxy/registry", content="{}", headers=JSON_BODY)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body == {"plate_number": "ZMM3707", "make": "FIAT AUTO SPA PUNTO", "emporiki_onomasia_ochimatos": "PUNTO",
                    "colour": "ΑΣΗΜΙ", "kylindrismos": "1368"}  # one object, five fields
    assert "ZFA18800000629998" not in resp.text and "123477" not in resp.text and "is_insured" not in resp.text


def test_a_list_is_returned_when_the_one_object_option_is_switched_off(client, source, _isolated_db, answer):
    registry_id = _registry(_isolated_db)
    _build(client, registry_id)
    form = {"format": "json", "single_present": "1"}  # the box unticked: no "single" sent
    assert "Output set" in _flash(client.post("/transform/sources/%s/output" % registry_id, data=form, follow_redirects=False))
    body = _caller().post("/proxy/registry", content="{}", headers=JSON_BODY).json()
    assert isinstance(body, list) and len(body) == 1 and body[0]["make"] == "FIAT AUTO SPA PUNTO"
    client.post("/transform/sources/%s/output" % registry_id, data={**form, "single": "1"}, follow_redirects=False)
    assert isinstance(_caller().post("/proxy/registry", content="{}", headers=JSON_BODY).json(), dict)


def test_a_transform_that_cannot_be_applied_fails_closed_and_never_passes_the_raw_answer(client, source, _isolated_db, answer):
    registry_id = _registry(_isolated_db)
    _build(client, registry_id)
    for broken in (b"this is not json", b'{"something": "else entirely"}', b"[1, 2, 3]"):
        answer["resp"] = FakeUpstreamResponse(content=broken)
        resp = _caller().post("/proxy/registry", content="{}", headers=JSON_BODY)
        assert resp.status_code == 502 and "could not be reshaped" in resp.text
        assert broken.decode() not in resp.text


def test_a_provider_error_is_passed_on_unchanged_and_a_switched_off_transform_does_nothing(client, source, _isolated_db, answer):
    registry_id = _registry(_isolated_db)
    _build(client, registry_id)
    answer["resp"] = FakeUpstreamResponse(status_code=403, content=b'{"detail": "no"}')
    resp = _caller().post("/proxy/registry", content="{}", headers=JSON_BODY)
    assert resp.status_code == 403 and resp.json() == {"detail": "no"}
    answer["resp"] = FakeUpstreamResponse(content=json.dumps(ANSWER).encode())
    client.post("/transform/sources/%s/enabled" % registry_id, data={"enabled": "0"}, follow_redirects=False)
    raw = _caller().post("/proxy/registry", content="{}", headers=JSON_BODY).json()
    assert raw["data"]["document"]["statements"]["arithmos_plaisiou"] == "ZFA18800000629998"  # passes through as before


def test_a_source_without_a_transform_is_unchanged(client, source, _isolated_db, answer):
    _registry(_isolated_db)
    assert _caller().post("/proxy/registry", content="{}", headers=JSON_BODY).json() == ANSWER


def test_the_pipeline_pages_say_the_answers_are_reshaped(client, source, _isolated_db, answer):
    registry_id = _registry(_isolated_db)
    before = _row(client.get("/load").text, "registry")
    assert "asked on demand" in before and "nothing is stored" in before and "reshaped" not in before
    _build(client, registry_id)
    after = _row(client.get("/load").text, "registry")
    assert "reshaped by the" in after and "nothing is stored" in after
    assert 'href="/transform/sources/%s"' % registry_id in after
    overview = client.get("/transform").text
    assert "acts on live answers" in overview and "/transform/sources/%s" % registry_id in overview
    page = client.get("/transform/sources/%s" % registry_id).text
    assert "reshaped" in page and "Run it again on earlier files" not in page and "Recent outputs" not in page  # nothing is stored for an API


def test_an_ordinary_file_source_page_is_unchanged(client, source):
    _go(client, source, "create")
    page = client.get("/transform/sources/%s" % source["id"]).text
    assert "Sample answer" not in page and "Run it again on earlier files" in page and "Recent outputs" in page


def test_the_engine_returns_one_object_only_when_asked_and_only_for_one_record():
    one, two = [{"a": 1}], [{"a": 1}, {"a": 2}]
    assert json.loads(te.write_output(one, "json")) == [{"a": 1}]
    assert json.loads(te.write_output(one, "json", single=True)) == {"a": 1}
    assert json.loads(te.write_output(two, "json", single=True)) == two
    assert json.loads(te.write_output([], "json", single=True)) == []


def test_the_chosen_output_format_is_what_callers_get_when_the_transform_is_on_and_a_warning_shows_when_it_is_off(client, source, _isolated_db, answer):
    registry_id = _registry(_isolated_db)
    _build(client, registry_id, enable=False)
    client.post("/transform/sources/%s/output" % registry_id, data={"format": "xml", "root": "vehicle", "record": "item", "single_present": "1", "single": "1"}, follow_redirects=False)
    assert "<strong>The transform is switched off" in client.get("/transform/sources/%s" % registry_id).text
    assert _caller().post("/proxy/registry", content="{}", headers=JSON_BODY).headers["content-type"].startswith("application/json")  # off: untouched
    client.post("/transform/sources/%s/enabled" % registry_id, data={"enabled": "1"}, follow_redirects=False)
    resp = _caller().post("/proxy/registry", content="{}", headers=JSON_BODY)
    assert resp.headers["content-type"].startswith("application/xml") or resp.headers["content-type"].startswith("text/xml")
    assert resp.text.lstrip().startswith("<?xml") or "<vehicle" in resp.text
    assert "<strong>The transform is switched off" not in client.get("/transform/sources/%s" % registry_id).text
