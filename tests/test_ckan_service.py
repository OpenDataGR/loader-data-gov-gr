"""A data service on data.gov.gr: a CKAN dataset of type ``data-service`` that describes a published pipeline of an API source (endpoint, how to call
it, licence, themes ...), created, updated and removed as a person -- against a fake CKAN."""

import json

import pytest

from providerharvest_service import ckan_client, ckan_load, ckan_vocab
from providerharvest_service.models import CkanTarget, ProviderSource, Transform
from tests.test_ckan_load import BASE, FakeCkan
from tests.test_transform_ui import _isolated_db  # noqa: F401  (a config database for the defaults test)

URL = BASE + "/data/opencar-data"


class ServiceCkan(FakeCkan):
    """FakeCkan plus what a data service needs: package_create (a name can be taken), package_patch, package_delete."""

    def __init__(self):
        super().__init__()
        self.packages, self.taken = {}, set()

    def call(self, action, data, files=None):
        if action in ("package_create", "package_patch", "package_delete"):
            self.calls.append((action, data, files))
            if action in self.fail:
                raise self.fail[action]
            if action == "package_create":
                if data["name"] in self.taken:
                    raise ckan_client.CKANValidationError(action, "{'name': ['That URL is already in use.']}")
                pid = "svc%d" % (len(self.packages) + 1)
                self.packages[pid] = dict(data, id=pid)
                self.taken.add(data["name"])
                return {"id": pid, "name": data["name"]}
            if data["id"] not in self.packages:
                raise ckan_client.CKANNotFound(action, "Not found")
            if action == "package_patch":
                self.packages[data["id"]].update(data)
                return self.packages[data["id"]]
            del self.packages[data["id"]]
            return None
        return super().call(action, data, files)


def _source(transport="http", **extra):
    return ProviderSource(id="s1", name="opencar-data", title="Opencar data", transport_type=transport,
                          config=json.dumps({"proxy_rate_limit_per_ip": 5, "proxy_method": "POST"}), **extra)


def _pipeline(name="default", *, published=True, enabled=False, swap=False, template=None, output="json"):
    return Transform(id="p1", source_id="s1", name=name, steps="[]", published=published, enabled=enabled, output_format=output,
                     request_swap=swap, request_template=template)


META = {"title": "Opencar vehicle lookup", "description": "Look a vehicle up by its plate.", "endpoint_description": "Call it.", "contact_email": "ops@example.gr",
        "contact_name": "Opencar team", "publisher_name": "Ministry", "themes": ["TRAN"]}


def _target(meta=None, **extra):
    return CkanTarget(source_id="s1", pipeline_id="p1", mode="service", organization_id="org1", dataset_id="", resource_name="x", ckan_user="user_abc",
                      service_meta=json.dumps(META if meta is None else meta), **extra)


# -- the details ----------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_details_are_cleaned_and_filled_with_the_defaults():
    meta = ckan_load.normalise_meta({"title": "  Lookup  ", "themes": "TRAN, GOVE", "documentation": "https://a.example/doc\nhttps://b.example/doc", "tags": "API, vehicles"})
    assert meta["title"] == "Lookup" and meta["themes"] == ["TRAN", "GOVE"] and meta["tags"] == ["API", "vehicles"]
    assert meta["documentation"] == ["https://a.example/doc", "https://b.example/doc"]
    assert (meta["access_rights"], meta["license"], meta["format"], meta["publisher_type"]) == ("PUBLIC", "CC_BY_4_0", "JSON", "NationalAuthority")
    assert meta["legislation"] == ["https://eur-lex.europa.eu/eli/dir/2019/1024/oj/eng"]


@pytest.mark.parametrize("raw,message", [
    ({"title": " "}, "Give the service a name"),
    ({"title": "x" * 201}, "longer than 200"),
    ({"title": "x", "access_rights": "SECRET"}, "access rights"),
    ({"title": "x", "format": "DOCX"}, "format"),
    ({"title": "x", "license": "MIT"}, "licence"),
    ({"title": "x", "themes": ["NOPE"]}, "theme"),
    ({"title": "x", "hvd_categories": ["nope"]}, "high-value"),
    ({"title": "x", "documentation": "not a link"}, "must be links"),
    ({"title": "x", "contact_email": "no-at-sign"}, "not an e-mail"),
    ({"title": "x", "publisher_type": "Wizard"}, "publisher type"),
])
def test_a_wrong_detail_is_explained(raw, message):
    with pytest.raises(ckan_load.CkanLoadError, match=message):
        ckan_load.normalise_meta(raw)


def test_a_licence_outside_the_list_can_be_given_as_a_link():
    assert ckan_load.normalise_meta({"title": "x", "license": "https://opendefinition.org/licenses/odc-by/"})["license"] == "https://opendefinition.org/licenses/odc-by/"


def test_the_vocabulary_values_become_the_eus_uris_and_back():
    assert ckan_vocab.uri("license", "CC_BY_4_0") == "http://publications.europa.eu/resource/authority/licence/CC_BY_4_0"
    assert ckan_vocab.uri("theme", "TRAN") == "http://publications.europa.eu/resource/authority/data-theme/TRAN"
    assert ckan_vocab.uri("hvd_category", "c_ac64a52d") == "http://data.europa.eu/bna/c_ac64a52d"
    assert ckan_vocab.uri("license", "https://x.example/l") == "https://x.example/l"  # a link is kept
    assert ckan_vocab.code_of("format", "http://publications.europa.eu/resource/authority/file-type/XML") == "XML"
    assert len(ckan_vocab.THEMES) == 13 and "PUBLIC" in ckan_vocab.codes("access_rights")


# -- the endpoint description ------------------------------------------------------------------------------------------------------------------------------

def test_the_endpoint_description_says_how_to_call_a_service_that_takes_a_get_with_parameters():
    pipeline = _pipeline(swap=True, template='{"document": {"statements": {"platenumber": "{plate}", "kind": "{kind}"}}}', enabled=True, output="xml")
    text = ckan_load.describe_endpoint(_source(), pipeline, URL)
    assert "HTTP GET: %s?plate=<plate>&kind=<kind>" % URL in text and "Parameters (all required): plate, kind." in text
    assert "answers with XML" in text and "at most 5 requests per minute from one address" in text


def test_the_endpoint_description_for_a_post_and_for_a_pass_through_get():
    assert "HTTP POST to %s" % URL in ckan_load.describe_endpoint(_source(), _pipeline(), URL) and "the provider's own answer (JSON)" in ckan_load.describe_endpoint(_source(), _pipeline(), URL)
    get_source = ProviderSource(id="s1", name="feed", title="Feed", transport_type="http", config="{}")
    text = ckan_load.describe_endpoint(get_source, _pipeline(enabled=True), URL)
    assert "HTTP GET: %s (query parameters are passed on" % URL in text and "at most 30 requests" in text


# -- creating, updating, removing ----------------------------------------------------------------------------------------------------------------------------

def test_creating_a_service_sends_a_data_service_dataset_with_the_endpoint_and_the_details():
    ckan, target = ServiceCkan(), _target()
    ckan_load.push(target, _source(), _pipeline(), ckan, base_url=BASE)
    (action, data, files), = [c for c in ckan.calls if c[0] == "package_create"]
    assert files is None and data["type"] == "data-service" and data["name"] == "opencar-data" and data["title"] == "Opencar vehicle lookup"
    assert data["owner_org"] == "org1" and data["private"] is False and data["endpoint_url"] == [URL] and data["endpoint_description"] == ["Call it."]
    assert data["access_rights"].endswith("/access-right/PUBLIC") and data["license"].endswith("/licence/CC_BY_4_0") and data["format"].endswith("/file-type/JSON")
    assert data["theme"] == ["http://publications.europa.eu/resource/authority/data-theme/TRAN"] and data["tags"] == [{"name": "API"}]
    assert data["contact"] == [{"name": "Opencar team", "email": "ops@example.gr", "uri": "", "url": ""}]
    assert data["publisher"][0]["name"] == "Ministry" and data["publisher"][0]["type"] == "http://purl.org/adms/publishertype/NationalAuthority"
    assert target.resource_id == target.dataset_id == "svc1" and target.dataset_name == "opencar-data" and target.last_status == "ok"


def test_an_address_already_taken_is_avoided_and_a_pipeline_gets_its_own():
    ckan = ServiceCkan()
    ckan.taken.add("opencar-data")
    ckan_load.push(_target(), _source(), _pipeline(), ckan, base_url=BASE)
    assert ckan.packages["svc1"]["name"] == "opencar-data-2"
    other = _target()
    ckan_load.push(other, _source(), _pipeline("lookup"), ckan, base_url=BASE)
    assert ckan.packages["svc2"]["name"] == "opencar-data-lookup" and ckan.packages["svc2"]["endpoint_url"] == [URL + "/lookup"]


def test_a_second_push_updates_the_same_entry_and_keeps_its_address():
    ckan, target = ServiceCkan(), _target()
    ckan_load.push(target, _source(), _pipeline(), ckan, base_url=BASE)
    target.service_meta = json.dumps(dict(META, title="Renamed"))
    ckan_load.push(target, _source(), _pipeline(), ckan, base_url=BASE)
    assert [c[0] for c in ckan.calls if c[0].startswith("package_")] == ["package_create", "package_patch"]
    patch = ckan.calls[-1][1]
    assert patch["id"] == "svc1" and patch["title"] == "Renamed" and "type" not in patch and "name" not in patch
    assert target.dataset_title == "Renamed" and len(ckan.packages) == 1


def test_an_entry_deleted_in_ckan_is_made_again():
    ckan, target = ServiceCkan(), _target(resource_id="gone")
    ckan_load.push(target, _source(), _pipeline(), ckan, base_url=BASE)
    assert [c[0] for c in ckan.calls if c[0].startswith("package_")] == ["package_patch", "package_create"] and target.resource_id == "svc1"


def test_a_service_needs_a_published_pipeline_an_organization_and_good_details():
    ckan = ServiceCkan()
    with pytest.raises(ckan_load.CkanLoadError, match="Publish the pipeline first"):
        ckan_load.push(_target(), _source(), _pipeline(published=False), ckan, base_url=BASE)
    no_org = _target()
    no_org.organization_id = None
    with pytest.raises(ckan_load.CkanLoadError, match="Choose the organization"):
        ckan_load.push(no_org, _source(), _pipeline(), ckan, base_url=BASE)
    with pytest.raises(ckan_load.CkanLoadError, match="Give the service a name"):
        ckan_load.push(_target({"title": ""}), _source(), _pipeline(), ckan, base_url=BASE)
    with pytest.raises(ckan_load.CkanLoadError, match="LOADER_PUBLIC_BASE_URL"):
        ckan_load.push(_target(), _source(), _pipeline(), ckan, base_url="")
    assert ckan.calls == []


def test_what_ckan_refuses_for_a_service_is_told_in_words():
    ckan, target = ServiceCkan(), _target()
    ckan.fail["package_create"] = ckan_client.CKANNotAuthorized("package_create", "no")
    with pytest.raises(ckan_load.CkanLoadError, match="may not change that dataset"):
        ckan_load.push(target, _source(), _pipeline(), ckan, base_url=BASE)
    assert target.last_status == "error" and target.resource_id is None


def test_removing_a_service_deletes_the_entry_not_a_resource():
    ckan, target = ServiceCkan(), _target()
    ckan_load.push(target, _source(), _pipeline(), ckan, base_url=BASE)
    assert ckan_load.remove(target, ckan) is True and ckan.packages == {} and target.resource_id is None
    assert [c[0] for c in ckan.calls][-1] == "package_delete" and not any(c[0] == "resource_delete" for c in ckan.calls)


# -- remembering what an organization chose --------------------------------------------------------------------------------------------------------------------

def test_the_details_an_organization_chose_are_remembered_for_its_next_service(_isolated_db):
    with _isolated_db() as session:
        assert ckan_load.get_defaults(session, "org1")["license"] == "CC_BY_4_0"  # nothing yet: the built-in defaults
        ckan_load.save_defaults(session, "org1", dict(ckan_load.normalise_meta(dict(META, license="CC0", themes=["TRAN", "GOVE"]))), "k")
        session.commit()
        saved = ckan_load.get_defaults(session, "org1")
        assert saved["license"] == "CC0" and saved["themes"] == ["TRAN", "GOVE"] and saved["contact_email"] == "ops@example.gr"
        assert saved["title"] == "" and saved["endpoint_description"] == ""  # a title and an endpoint text belong to one service
        assert ckan_load.get_defaults(session, "org2")["license"] == "CC_BY_4_0"  # another organization is untouched
        ckan_load.save_defaults(session, "org1", {"license": "ODC_BY"}, "k")
        session.commit()
        assert ckan_load.get_defaults(session, "org1")["license"] == "ODC_BY"  # updated in place
