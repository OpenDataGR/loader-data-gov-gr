"""The CKAN destination of the Load Box: finding a dataset, creating / updating / removing a pipeline's resource as a person, link and upload
modes -- against a fake CKAN, never a real one."""

import types

import pytest

from providerharvest_service import ckan_client, ckan_load
from providerharvest_service.models import CkanTarget, ProviderSource, Transform
from tests.test_ckan_client import FakeResponse, FakeSession

BASE = "https://loader.example.gr"


class FakeCkan:
    """Just enough of CKAN's Action API: organizations, a dataset search, resource create / update / delete."""

    def __init__(self):
        self.calls, self.resources, self.fail = [], {}, {}

    def call(self, action, data, files=None):
        self.calls.append((action, data, files))
        if action in self.fail:
            raise self.fail[action]
        if action == "organization_list_for_user":
            return [{"id": "org2", "name": "air", "title": "Air"}, {"id": "org1", "name": "traffic", "title": "Traffic"}]
        if action == "package_search":
            return {"results": [{"id": "ds1", "name": "attica-traffic", "title": "Attica traffic", "organization": {"title": "Traffic"}, "owner_org": "org1",
                                 "private": True, "state": "draft", "num_resources": 3, "metadata_modified": "2026-10-12T05:04:21.123456"},
                                {"id": "ds2", "name": "air", "owner_org": "org2", "resources": [{"id": "r"}]}]}
        if action == "package_show":
            return {"id": data["id"], "name": "attica-traffic", "title": "Attica traffic", "organization": {"title": "Traffic"},
                    "owner_org": "org2" if data["id"] == "ds2" else "org1"}
        if action == "resource_create":
            rid = "res%d" % (len(self.resources) + 1)
            self.resources[rid] = dict(data, id=rid)
            return {"id": rid}
        if action == "resource_update":
            if data["id"] not in self.resources:
                raise ckan_client.CKANNotFound(action, "Not found")
            self.resources[data["id"]].update(data)
            return self.resources[data["id"]]
        if action == "resource_delete":
            if data["id"] not in self.resources:
                raise ckan_client.CKANNotFound(action, "Not found")
            del self.resources[data["id"]]
            return None
        raise AssertionError("unexpected CKAN call %s" % action)


def _source(transport="push"):
    return ProviderSource(id="s1", name="attica-traffic-data", title="Attica", transport_type=transport)


def _pipeline(name="default", *, published=True, output="json", enabled=True):
    return Transform(id="p1", source_id="s1", name=name, steps="[]", published=published, output_format=output, enabled=enabled)


def _target(mode="link", **extra):
    return CkanTarget(source_id="s1", pipeline_id="p1", mode=mode, dataset_id="ds1", resource_name="Traffic", ckan_user="user_abc", **extra)


def _output(content=b'{"a": 1}', filename="Detector.json"):
    return types.SimpleNamespace(id="out1", content=content, filename=filename, content_type="application/json")


# -- finding the dataset -------------------------------------------------------------------------------------------------------------------------------

def test_the_search_is_limited_to_the_organizations_the_person_can_add_datasets_to():
    ckan = FakeCkan()
    found = ckan_load.search_datasets(ckan, "user_abc", "traffic")
    assert [(d.id, d.title, d.organization, d.owner_org) for d in found] == [("ds1", "Attica traffic", "Traffic", "org1"), ("ds2", "air", "", "org2")]
    first, second = found  # and what each says about itself: private or public, its state, its resources, when it last changed
    assert (first.private, first.state, first.num_resources, first.modified) == (True, "draft", 3, "2026-10-12")
    assert (second.private, second.state, second.num_resources, second.modified) == (False, "active", 1, "")  # (counted from its resources when CKAN gives no count)
    (_, orgs_call, _), (_, search_call, _) = ckan.calls
    assert orgs_call == {"id": "user_abc", "permission": "create_dataset"}  # an editor counts, not only an administrator
    assert search_call["fq"] == "owner_org:(org2 OR org1)" and search_call["q"] == "traffic" and search_call["include_private"] is True


def test_a_person_with_no_organization_finds_nothing_and_nothing_is_searched():
    class Nobody(FakeCkan):
        def call(self, action, data, files=None):
            return [] if action == "organization_list_for_user" else super().call(action, data, files)

    ckan = Nobody()
    assert ckan_load.search_datasets(ckan, "user_abc") == []


def test_a_search_that_ckan_refuses_is_explained():
    ckan = FakeCkan()
    ckan.fail["package_search"] = ckan_client.CKANActionError("package_search", "boom")
    with pytest.raises(ckan_load.CkanLoadError, match="could not be reached or failed"):
        ckan_load.search_datasets(ckan, "user_abc")


# -- link mode ---------------------------------------------------------------------------------------------------------------------------------------------

def test_link_mode_creates_a_resource_that_points_at_the_published_address_then_updates_it():
    ckan, target, pipeline = FakeCkan(), _target(), _pipeline()
    ckan_load.push(target, _source(), pipeline, ckan, base_url=BASE)
    (create,) = [c for c in ckan.calls if c[0] == "resource_create"]
    assert create[1] == {"package_id": "ds1", "name": "Traffic", "description": "", "url": BASE + "/data/attica-traffic-data", "format": "JSON"} and create[2] is None
    assert target.resource_id == "res1" and target.last_status == "ok" and target.last_error is None and target.last_pushed_at is not None
    ckan_load.push(target, _source(), pipeline, ckan, base_url=BASE)
    assert [c[0] for c in ckan.calls] == ["resource_create", "resource_update"] and ckan.calls[1][1]["id"] == "res1"
    assert len(ckan.resources) == 1  # one resource, updated in place


def test_another_pipeline_links_to_its_own_address_and_the_format_follows_what_it_writes():
    ckan, target = FakeCkan(), _target()
    ckan_load.push(target, _source(), _pipeline("table", output="csv"), ckan, base_url=BASE)
    assert ckan.resources["res1"]["url"] == BASE + "/data/attica-traffic-data/table" and ckan.resources["res1"]["format"] == "CSV"


def test_an_api_source_pipeline_that_passes_the_answer_through_is_labelled_json():
    ckan, target = FakeCkan(), _target()
    ckan_load.push(target, _source("http"), _pipeline(output="xml", enabled=False), ckan, base_url=BASE)
    assert ckan.resources["res1"]["format"] == "JSON"


def test_a_link_needs_a_published_pipeline_and_a_public_address_and_the_failure_is_recorded():
    ckan, target = FakeCkan(), _target()
    with pytest.raises(ckan_load.CkanLoadError, match="Publish the pipeline first"):
        ckan_load.push(target, _source(), _pipeline(published=False), ckan, base_url=BASE)
    assert target.last_status == "error" and "Publish the pipeline first" in target.last_error and ckan.calls == []
    with pytest.raises(ckan_load.CkanLoadError, match="LOADER_PUBLIC_BASE_URL"):
        ckan_load.push(target, _source(), _pipeline(), ckan, base_url="")


def test_a_resource_deleted_in_ckan_is_created_again():
    ckan, target = FakeCkan(), _target(resource_id="gone")
    ckan_load.push(target, _source(), _pipeline(), ckan, base_url=BASE)
    assert [c[0] for c in ckan.calls] == ["resource_update", "resource_create"] and target.resource_id == "res1"


@pytest.mark.parametrize("error,message", [
    (ckan_client.CKANNotAuthorized("resource_create", "no"), "may not change that dataset"),
    (ckan_client.CKANNotFound("resource_create", "no"), "does not know that dataset"),
    (ckan_client.CKANValidationError("resource_create", "bad name"), "CKAN refused it"),
])
def test_what_ckan_refuses_is_told_in_words_and_kept_on_the_target(error, message):
    ckan, target = FakeCkan(), _target()
    ckan.fail["resource_create"] = error
    with pytest.raises(ckan_load.CkanLoadError, match=message):
        ckan_load.push(target, _source(), _pipeline(), ckan, base_url=BASE)
    assert target.last_status == "error" and message in target.last_error and target.resource_id is None


# -- upload mode -------------------------------------------------------------------------------------------------------------------------------------------

def test_upload_mode_sends_the_newest_output_as_a_file_with_no_url():
    ckan, target = FakeCkan(), _target("upload")
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(), base_url=BASE)
    action, data, files = ckan.calls[0]
    assert action == "resource_create" and data["url"] == "" and data["format"] == "JSON" and data["package_id"] == "ds1"
    assert files == {"upload": ("Detector.json", b'{"a": 1}', "application/json")} and target.last_output_id == "out1"
    ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(b"[2]"), base_url=BASE)
    assert ckan.calls[1][0] == "resource_update" and ckan.calls[1][2]["upload"][1] == b"[2]"


def test_upload_mode_needs_an_output_and_refuses_one_that_is_too_big(monkeypatch):
    ckan, target = FakeCkan(), _target("upload")
    with pytest.raises(ckan_load.CkanLoadError, match="no output to upload"):
        ckan_load.push(target, _source(), _pipeline(), ckan, output=None)
    monkeypatch.setattr(ckan_load, "MAX_UPLOAD_BYTES", 5)
    with pytest.raises(ckan_load.CkanLoadError, match="too big"):
        ckan_load.push(target, _source(), _pipeline(), ckan, output=_output(b"123456"))
    assert ckan.calls == []


def test_an_unknown_mode_is_refused():
    with pytest.raises(ckan_load.CkanLoadError, match="Unknown way"):
        ckan_load.push(_target("teleport"), _source(), _pipeline(), FakeCkan())


# -- removing ------------------------------------------------------------------------------------------------------------------------------------------------

def test_removing_deletes_the_resource_and_forgets_it():
    ckan, target = FakeCkan(), _target()
    ckan_load.push(target, _source(), _pipeline(), ckan, base_url=BASE)
    assert ckan_load.remove(target, ckan) is True and ckan.resources == {} and target.resource_id is None
    assert ckan_load.remove(target, ckan) is False  # nothing left to remove


def test_a_resource_that_ckan_no_longer_has_counts_as_removed_and_other_failures_are_explained():
    ckan, target = FakeCkan(), _target(resource_id="gone")
    assert ckan_load.remove(target, ckan) is True and target.resource_id is None
    target.resource_id = "res9"
    ckan.fail["resource_delete"] = ckan_client.CKANNotAuthorized("resource_delete", "no")
    with pytest.raises(ckan_load.CkanLoadError, match="may not change"):
        ckan_load.remove(target, ckan)
    assert target.resource_id == "res9"  # still there: it was not removed


# -- the client ----------------------------------------------------------------------------------------------------------------------------------------------

def test_a_call_with_a_file_is_sent_as_a_multipart_form_and_a_plain_call_as_json():
    session = FakeSession(FakeResponse(json_body={"success": True, "result": {"id": "r"}}))
    client = ckan_client.CKANActionClient(BASE, "tok", session=session)
    client.call("resource_create", {"package_id": "ds1", "url": "", "name": "x"}, files={"upload": ("a.csv", b"1,2", "text/csv")})
    url, kwargs = session.calls[0]
    assert url.endswith("/api/3/action/resource_create") and "json" not in kwargs and kwargs["files"]["upload"][0] == "a.csv"
    assert kwargs["data"] == {"package_id": "ds1", "url": "", "name": "x"} and kwargs["headers"] == {"Authorization": "tok"}
    client.call("package_show", {"id": "ds1"})
    assert session.calls[1][1]["json"] == {"id": "ds1"} and "files" not in session.calls[1][1]


# -- organization first, then the dataset within it ----------------------------------------------------------------------------------------------------

def test_the_organizations_are_those_the_person_may_add_to_sorted_by_title():
    found = ckan_load.list_organizations(FakeCkan(), "user_abc")
    assert [(o.id, o.title) for o in found] == [("org2", "Air"), ("org1", "Traffic")]


def test_a_dataset_search_can_be_limited_to_one_organization_but_never_to_one_the_person_may_not_use():
    ckan = FakeCkan()
    ckan_load.search_datasets(ckan, "user_abc", "x", organization_id="org2")
    assert ckan.calls[-1][1]["fq"] == "owner_org:(org2)"
    with pytest.raises(ckan_load.CkanLoadError, match="may not add datasets to that organization"):
        ckan_load.search_datasets(ckan, "user_abc", organization_id="org-not-mine")


def test_a_failing_organization_list_is_explained():
    ckan = FakeCkan()
    ckan.fail["organization_list_for_user"] = ckan_client.CKANActionError("organization_list_for_user", "boom")
    with pytest.raises(ckan_load.CkanLoadError, match="could not be reached or failed"):
        ckan_load.list_organizations(ckan, "user_abc")
