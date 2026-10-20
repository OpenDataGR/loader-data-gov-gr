"""The documentation of a published address: an OpenAPI description and a Swagger page, public, only for a pipeline that is published of an active source."""

import json
from types import SimpleNamespace

from providerharvest_service import api_docs
from providerharvest_service.models import ProviderSource, Transform
from tests.test_live_transform import _http_source, _isolated_db, _isolated_harvest_db, _master_key, client, source  # noqa: F401
from tests.test_pipeline_publishing import _publish, _stranger
from tests.test_pipelines_ui import _add, _pipeline, _setup
from tests.test_transform_ui import _go, _upload_data


def _stored(client, source):
    _setup(client, source)
    _go(client, source, "enabled", data={"enabled": "1"})
    _add(client, source, "table")
    _pipeline(client, source, "table", "output", data={"format": "csv", "delimiter": ","})
    _pipeline(client, source, "table", "enabled", data={"enabled": "1"})
    _upload_data(client, source)
    _publish(client, source["id"])
    _publish(client, source["id"], "table")


def test_the_page_and_the_description_are_public_and_only_for_what_is_published(client, source):
    stranger = _stranger()
    assert stranger.get("/api-docs/acme-push").status_code == 404 and stranger.get("/api-docs/acme-push/openapi.json").status_code == 404  # nothing is published yet
    _stored(client, source)
    page = stranger.get("/api-docs/acme-push")
    assert page.status_code == 200 and "swagger-ui-bundle.js" in page.text and '"/api-docs/acme-push/openapi.json"' in page.text
    assert stranger.get("/api-docs/acme-push/table").status_code == 200 and '"/api-docs/acme-push/table/openapi.json"' in stranger.get("/api-docs/acme-push/table").text
    assert stranger.get("/api-docs/acme-push/ghost").status_code == 404 and stranger.get("/api-docs/nobody").status_code == 404
    _publish(client, source["id"], "table", on=False)
    assert stranger.get("/api-docs/acme-push/table").status_code == 404 and stranger.get("/api-docs/acme-push").status_code == 200


def test_a_source_that_stores_data_is_described_with_the_columns_of_its_newest_output(client, source):
    _stored(client, source)
    spec = _stranger().get("/api-docs/acme-push/openapi.json").json()
    assert spec["openapi"] == "3.1.0" and list(spec["paths"]) == ["/data/acme-push"]
    operation = spec["paths"]["/data/acme-push"]["get"]
    assert "newest successful output" in operation["description"] and "60 requests a minute" in operation["description"] and "parameters" not in operation
    ok = operation["responses"]["200"]
    media = ok["content"]["application/json"]
    assert media["schema"]["type"] == "array" and media["schema"]["items"]["properties"]["id"] == {"type": "string"} and media["example"][0]["id"]
    assert "ETag" in ok["headers"] and "304" in operation["responses"] and "404" in operation["responses"] and "429" in operation["responses"]
    csv = _stranger().get("/api-docs/acme-push/table/openapi.json").json()["paths"]["/data/acme-push/table"]["get"]["responses"]["200"]["content"]["text/csv"]
    assert csv["example"].splitlines()[0].startswith("id,") and "CSV with a header row" in csv["example"] + _stranger().get("/api-docs/acme-push/table/openapi.json").text


def test_an_api_source_lists_the_parameters_callers_send(client, source, _isolated_db):
    source_id = _http_source(_isolated_db, "registry", post=True)
    from tests.test_request_swap import REGISTRY_BODY, _swap

    client.post("/transform/sources/%s/create" % source_id, follow_redirects=False)
    _swap(client, source_id)
    spec = _stranger().get("/api-docs/registry/openapi.json").json()
    operation = spec["paths"]["/data/registry"]["get"]  # (the provider takes a POST: callers send a GET)
    assert [(p["name"], p["in"], p["required"]) for p in operation["parameters"]] == [("plate", "query", True)] and "nothing is stored" in operation["description"]
    assert "502" in operation["responses"] and "ETag" not in json.dumps(operation["responses"]) and REGISTRY_BODY


def test_a_pass_through_api_source_does_not_invent_a_shape(client, source, _isolated_db):
    _http_source(_isolated_db, "openfeed")
    operation = _stranger().get("/api-docs/openfeed/openapi.json").json()["paths"]["/data/openfeed"]["get"]
    assert "passed on to the provider" in operation["description"] or "Query parameters are passed on" in operation["description"]
    assert "unchanged" in operation["responses"]["200"]["content"]["application/json"]["schema"]["description"]


def test_the_description_helpers_work_without_a_database():
    pipeline = Transform(id="p", source_id="s", name="table", steps="[]", published=True, enabled=True, output_format="xml")
    stored = ProviderSource(id="s", name="feed", title="Feed", transport_type="push")
    assert api_docs.docs_path(stored, pipeline) == "/api-docs/feed/table"
    assert api_docs.docs_path(stored, SimpleNamespace(name="default")) == "/api-docs/feed"
    spec = api_docs.build_spec(stored, pipeline, "https://loader.example.gr")
    assert spec["servers"] == [{"url": "https://loader.example.gr"}] and "application/xml" in spec["paths"]["/data/feed/table"]["get"]["responses"]["200"]["content"]
    assert "swagger-ui-dist@%s" % api_docs.SWAGGER_UI in api_docs.page('A <b>title</b>', "/x/openapi.json") and "&lt;b&gt;" in api_docs.page('A <b>title</b>', "/x")


def test_the_load_box_links_to_the_documentation_and_a_data_service_carries_it(client, source):
    _stored(client, source)
    assert 'href="/api-docs/acme-push"' in client.get("/load").text and 'href="/api-docs/acme-push/table"' in client.get("/load").text
