import pytest

from providerharvest_service.ckan_client import (
    CKANActionClient,
    CKANActionError,
    CKANNotAuthorized,
    CKANNotFound,
    CKANValidationError,
)
from providerharvest_service.engine.loaders.datastore_loader import DataStoreLoader
from providerharvest_service.engine.mapping import FieldMappingProfile, FieldMappingRule


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, raises_on_json=False):
        self.status_code = status_code
        self._json_body = json_body
        self._raises_on_json = raises_on_json

    def json(self):
        if self._raises_on_json:
            raise ValueError("not json")
        return self._json_body


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


def _client(response, base_url="https://data.gov.gr", token="tok-123"):
    session = FakeSession(response)
    return CKANActionClient(base_url, token, session=session), session


def test_successful_call_returns_result():
    response = FakeResponse(json_body={"success": True, "result": {"id": "abc"}})
    client, session = _client(response)

    result = client.call("package_show", {"id": "abc"})

    assert result == {"id": "abc"}


def test_request_url_and_auth_header():
    response = FakeResponse(json_body={"success": True, "result": None})
    client, session = _client(response, base_url="https://data.gov.gr/")

    client.call("package_show", {"id": "abc"})

    url, kwargs = session.calls[0]
    assert url == "https://data.gov.gr/api/3/action/package_show"
    assert kwargs["headers"]["Authorization"] == "tok-123"
    assert kwargs["json"] == {"id": "abc"}


def test_get_action_matches_toolkit_signature_and_ignores_context():
    response = FakeResponse(json_body={"success": True, "result": {"ok": 1}})
    client, session = _client(response)

    action = client.get_action("organization_list_for_user")
    result = action({"user": "whoever"}, {"id": "someone-else"})

    assert result == {"ok": 1}


@pytest.mark.parametrize("ckan_type,expected_cls", [
    ("Validation Error", CKANValidationError),
    ("Authorization Error", CKANNotAuthorized),
    ("Not Found Error", CKANNotFound),
    ("Some Future CKAN Error Type", CKANActionError),
])
def test_error_types_map_to_the_right_exception(ckan_type, expected_cls):
    response = FakeResponse(
        status_code=409,
        json_body={"success": False, "error": {"__type": ckan_type, "message": "nope"}},
    )
    client, session = _client(response)

    with pytest.raises(expected_cls) as excinfo:
        client.call("provider_source_create", {})

    assert excinfo.value.ckan_type == ckan_type
    assert excinfo.value.action_name == "provider_source_create"
    assert "nope" in str(excinfo.value)


def test_validation_error_carries_field_details_through():
    response = FakeResponse(
        status_code=409,
        json_body={
            "success": False,
            "error": {
                "__type": "Validation Error",
                "message": "Invalid",
                "name": ["A source with this name already exists"],
            },
        },
    )
    client, session = _client(response)

    with pytest.raises(CKANValidationError) as excinfo:
        client.call("provider_source_create", {"name": "dup"})

    assert excinfo.value.details["name"] == ["A source with this name already exists"]


def test_non_json_response_raises_action_error():
    response = FakeResponse(status_code=502, raises_on_json=True)
    client, session = _client(response)

    with pytest.raises(CKANActionError) as excinfo:
        client.call("package_show", {"id": "abc"})

    assert "502" in str(excinfo.value)


class QueuedFakeSession:
    """Same fake as above, but for a caller (DataStoreLoader) that makes
    more than one action call and expects a different response each
    time."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


def test_datastore_loader_works_unchanged_against_this_client():
    """The whole point of get_action() matching toolkit.get_action's
    shape: DataStoreLoader was written against CKAN's in-process action
    layer without ever importing it, so it should load rows through a
    real CKANActionClient with no changes at all."""
    session = QueuedFakeSession([
        FakeResponse(json_body={"success": True, "result": {"created": True}}),
        FakeResponse(json_body={"success": True, "result": {"records": 2}}),
    ])
    client = CKANActionClient("https://data.gov.gr", "tok-123", session=session)
    loader = DataStoreLoader(client.get_action)
    profile = FieldMappingProfile(row_rules=[
        FieldMappingRule(ckan_field="id", source_path="id", field_type="integer",
                          is_primary_key=True),
        FieldMappingRule(ckan_field="name", source_path="name"),
    ])

    loader.ensure_datastore_schema({}, "resource-1", profile)
    loader.upsert_rows({}, "resource-1", [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])

    schema_url, schema_kwargs = session.calls[0]
    upsert_url, upsert_kwargs = session.calls[1]
    assert schema_url.endswith("/api/3/action/datastore_create")
    assert schema_kwargs["json"]["resource_id"] == "resource-1"
    assert schema_kwargs["json"]["primary_key"] == ["id"]
    assert upsert_url.endswith("/api/3/action/datastore_upsert")
    assert upsert_kwargs["json"]["records"] == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
