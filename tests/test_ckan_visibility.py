"""Making the dataset (or data service) of an output public or private from its page -- as the signed-in person, against a fake CKAN."""

import pytest

from providerharvest_service import ckan_client, ckan_load
from tests.test_ckan_datastore import DatastoreCkan, ckan as _unused_ckan  # noqa: F401
from tests.test_ckan_load import _target
from tests.test_ckan_ui import _connect, _page, _ready, _targets
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _flash, _isolated_db, _isolated_harvest_db, _master_key, client, source,
)


class VisibleCkan(DatastoreCkan):
    """DatastoreCkan that remembers whether each dataset is private (package_show / package_patch on a dataset it was not told about)."""

    def __init__(self):
        super().__init__()
        self.private = {"ds1": True}

    def call(self, action, data, files=None):
        if action == "package_show" and data["id"] in self.private:
            self.calls.append((action, data, files))
            if action in self.fail:
                raise self.fail[action]
            return {"id": data["id"], "name": "attica-traffic", "title": "Attica traffic", "organization": {"title": "Traffic"}, "owner_org": "org1", "private": self.private[data["id"]]}
        if action == "package_patch" and data["id"] in self.private:
            self.calls.append((action, data, files))
            if action in self.fail:
                raise self.fail[action]
            self.private[data["id"]] = data["private"]
            return {}
        return super().call(action, data, files)


@pytest.fixture
def ckan(monkeypatch):
    from providerharvest_service import ui_ckan
    from tests.test_ckan_load import BASE

    fake = VisibleCkan()
    monkeypatch.setenv("CKAN_BASE_URL", "https://ckan.example.gr")
    monkeypatch.setenv("CKAN_SYSADMIN_API_TOKEN", "sysadmin-token")
    monkeypatch.setenv("LOADER_PUBLIC_BASE_URL", BASE)
    monkeypatch.setattr(ckan_load, "user_client", lambda session, ckan_user: fake)
    monkeypatch.setattr(ui_ckan, "_identity", lambda request: "user_abc")
    return fake


def test_a_private_dataset_is_shown_as_private_and_can_be_made_public_and_back(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"], mode="upload")
    (target,) = _targets(_isolated_db)
    page = client.get(_page(source["id"])).text
    assert "<span>private</span>" in page and "Make public</button>" in page
    resp = client.post(_page(source["id"], tail="ckan/visibility"), data={"target_id": target.id, "private": "0"}, follow_redirects=False)
    assert "is now public (anyone can see it) in CKAN." in _flash(resp) and ckan.private["ds1"] is False
    assert [c[1] for c in ckan.calls if c[0] == "package_patch"] == [{"id": "ds1", "private": False}]
    page = client.get(_page(source["id"])).text
    assert "<span>public</span>" in page and "Make private</button>" in page and "Make the whole dataset private?" in page
    client.post(_page(source["id"], tail="ckan/visibility"), data={"target_id": target.id, "private": "1"}, follow_redirects=False)
    assert ckan.private["ds1"] is True


def test_what_ckan_refuses_is_told_and_nothing_changes(client, source, ckan, _isolated_db):
    _ready(client, source)
    _connect(client, source["id"], mode="upload")
    (target,) = _targets(_isolated_db)
    ckan.fail["package_patch"] = ckan_client.CKANNotAuthorized("package_patch", "no")
    resp = client.post(_page(source["id"], tail="ckan/visibility"), data={"target_id": target.id, "private": "0"}, follow_redirects=False)
    assert "The visibility could not be changed: CKAN says you may not change that dataset." in _flash(resp) and ckan.private["ds1"] is True


def test_nothing_is_offered_before_the_first_push_or_when_ckan_cannot_say(client, source, ckan, _isolated_db):
    _ready(client, source)
    assert "visibility" not in client.get(_page(source["id"])).text  # no output yet
    unpushed = _target("service")  # (a data service is the entry itself: until it is pushed there is nothing in CKAN)
    with pytest.raises(ckan_load.CkanLoadError, match="push the output first"):
        ckan_load.set_private(ckan, unpushed, False)
    ckan.fail["package_show"] = ckan_client.CKANActionError("package_show", "down")
    unpushed.resource_id = "svc9"
    ckan.private["svc9"] = True
    assert ckan_load.is_private(ckan, unpushed) is None


def test_a_data_service_is_changed_through_its_own_entry():
    fake = VisibleCkan()
    fake.private["svc1"] = False
    service = _target("service")
    service.resource_id = "svc1"
    assert ckan_load.is_private(fake, service) is False
    ckan_load.set_private(fake, service, True)
    assert fake.private["svc1"] is True
