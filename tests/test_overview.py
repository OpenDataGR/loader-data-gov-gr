"""The Overview: each source the person may see as a flow -- input, transform, output, where it goes -- scoped by CKAN organization."""

import re

import pytest

from providerharvest_service import ckan_client, ckan_load, overview, ui_ckan, ui_provider
from providerharvest_service.models import ProviderSource
from tests.test_bulk_outputs import _output
from tests.test_ckan_ui import _ready, ckan  # noqa: F401  (the ckan fixture: CKAN configured, one fake, the person is user_abc)
from tests.test_live_transform import _http_source
from tests.test_pipeline_publishing import _publish
from tests.test_pipelines_ui import _add, _pipeline
from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _go, _isolated_db, _isolated_harvest_db, _master_key, client, source,
)


@pytest.fixture(autouse=True)
def _harvest_factory(_isolated_harvest_db, monkeypatch):
    from providerharvest_service import harvest_db

    monkeypatch.setattr(harvest_db, "get_session_factory", lambda: _isolated_harvest_db)


def _organization(_isolated_db, source_id, org_id, title):
    with _isolated_db() as s:
        row = s.get(ProviderSource, source_id)
        row.organization_id, row.organization_title = org_id, title
        s.commit()


def _page(client):
    return client.get("/overview").text


def _flow(page, source_id):
    """The markup of one source's flow."""
    start = page.rindex("<article", 0, page.index('id="flow-%s"' % source_id))  # (its tag carries the state and the search text)
    return page[start:page.index("</article>", start)]


@pytest.fixture
def org_admin(monkeypatch):
    """The signed-in person is not a super admin: they administer 'org1' (Traffic) in CKAN."""
    monkeypatch.setattr(ui_provider, "_signed_in", lambda request: (False, "user_abc"))
    monkeypatch.setattr(ui_ckan, "_identity", lambda request: "user_abc")


# -- the picture ---------------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_tab_is_in_the_menu_and_a_source_is_drawn_from_input_to_where_it_goes(client, source, ckan, _isolated_db):
    _ready(client, source)
    client.post("/load/sources/%s/ckan" % source["id"], data={"mode": "link", "organization_id": "org1", "dataset_id": "ds1", "resource_name": "Traffic"})
    _organization(_isolated_db, source["id"], "org1", "Ministry of Transport")
    page = _page(client)
    assert '<a href="/overview" class="active">' in page and "Overview</a>" in page
    flow = _flow(page, source["id"])
    assert re.search(r'class="node node-input"[^>]*data-node="s"', flow) and "Acme push" in flow and "acme-push" in flow  # the input
    assert "Transform" in flow and "node-transform" in flow and ">default" in flow and "Read XML" in flow  # the transform, with its step
    assert "node-output" in flow and "JSON" in flow and "1 ok" in flow  # the output, with how it went
    assert "/data/acme-push" in flow and '<span class="state on">live</span>' in flow  # the published address
    assert "CKAN &middot; link" in flow and "Attica traffic" in flow and "in CKAN" in flow  # and its resource in CKAN


def test_the_boxes_are_wired_so_the_lines_connect_input_to_transform_to_output_to_the_destinations(client, source, ckan, _isolated_db):
    _ready(client, source)
    _add(client, source, "table")
    _pipeline(client, source, "table", "enabled", data={"enabled": "1"})
    _publish(client, source["id"], "table")
    client.post("/load/sources/%s/ckan" % source["id"], data={"mode": "link", "organization_id": "org1", "dataset_id": "ds1", "resource_name": "Traffic"})
    flow = _flow(_page(client), source["id"])
    assert re.search(r'data-node="s"\s+data-to="p0t p1t\s*"', flow)  # the input feeds both pipelines
    assert 'data-node="p0t" data-to="p0o"' in flow and 'data-node="p1t" data-to="p1o"' in flow
    assert re.search(r'data-node="p0o" data-to="p0d0 p0d1 "', flow) and re.search(r'data-node="p1o" data-to="p1d0 "', flow)  # the first goes to two places, the second to one
    for node in ("p0d0", "p0d1", "p1d0"):
        assert 'data-node="%s"' % node in flow


def test_the_transform_shows_whether_it_is_on_and_its_steps_and_a_pass_through_says_so(client, source, ckan, _isolated_db):
    _ready(client, source)
    registry = _http_source(_isolated_db, "opencar-data", post=True)
    page = _page(client)
    assert '<span class="state on">on</span>' in _flow(page, source["id"])
    api = _flow(page, registry)
    assert '<span class="state off">off</span>' in api and "no steps: passes it through" in api and "built when someone calls" in api
    assert "asked on demand, when someone calls" in api


def test_a_request_turned_around_is_marked_on_the_transform(client, source, ckan, _isolated_db):
    from tests.test_request_swap import REGISTRY_BODY

    registry = _http_source(_isolated_db, "opencar-data", post=True)
    client.post("/load/sources/%s/request" % registry, data={"mode": "swap", "template": REGISTRY_BODY}, follow_redirects=False)
    assert "request turned around" in _flow(_page(client), registry)


def test_a_pipeline_that_goes_nowhere_yet_says_so_and_a_source_without_a_pipeline_offers_to_set_one_up(client, source, ckan, _isolated_db):
    _ready(client, source)
    bare = _http_source(_isolated_db, "bare", published=False)
    page = _page(client)
    assert "Not delivered yet" not in _flow(page, source["id"])  # (it is published)
    _publish(client, source["id"], on=False)
    again = _flow(_page(client), source["id"])
    assert "Not delivered yet" in again and "Publish it in the Load Box" in again and 'data-node="p0g"' in again
    nothing = _flow(page, bare)
    assert "No pipeline yet" in nothing and 'href="/transform/sources/%s"' % bare in nothing and 'data-node="x0"' in nothing


def test_a_failed_output_counts_are_shown_and_mark_the_source_as_needing_attention(client, source, ckan, _isolated_db, _isolated_harvest_db):
    _ready(client, source)
    with _isolated_db() as s:
        from providerharvest_service import transform_runner

        transform_id = transform_runner.get_transform(s, source["id"]).id
    _output(_isolated_harvest_db, source["id"], transform_id, 9, status="error", minute=9)
    page = _page(client)
    flow = _flow(page, source["id"])
    assert '<span class="bad">1 failed</span>' in flow and 'data-flag="attention"' in flow
    assert re.search(r'Needs attention<span class="n">1</span>', page) and re.search(r'Running<span class="n">0</span>', page)


def test_the_state_of_each_source_decides_its_group_in_the_filter(client, source, ckan, _isolated_db):
    _ready(client, source)  # running
    paused = _http_source(_isolated_db, "paused-one", post=True)
    client.post("/admin/sources/%s/pause" % paused)
    idle = _http_source(_isolated_db, "idle-one", published=False)
    page = _page(client)
    assert 'data-flag="running"' in _flow(page, source["id"]) and 'data-flag="paused"' in _flow(page, paused) and 'data-flag="idle"' in _flow(page, idle)
    for chip in ("All<span class=\"n\">3</span>", "Running<span class=\"n\">1</span>", "Paused or waiting<span class=\"n\">1</span>", "Not delivering<span class=\"n\">1</span>"):
        assert chip in page


def test_the_sources_are_grouped_by_organization_and_the_ones_without_come_last_for_super_admins(client, source, ckan, _isolated_db):
    _ready(client, source)
    other = _http_source(_isolated_db, "zeta-api", post=True)
    plain = _http_source(_isolated_db, "plain-api", post=True)
    _organization(_isolated_db, source["id"], "org1", "Ministry of Transport")
    _organization(_isolated_db, other, "org3", "Air authority")
    page = _page(client)
    order = [page.index(marker) for marker in ('data-org="Air authority"', 'data-org="Ministry of Transport"', 'data-org="No organization"')]
    assert order == sorted(order) and "only super admins see these" in page
    assert page.index("flow-%s" % plain) > page.index('data-org="No organization"')
    assert '<option value="Ministry of Transport">' in page and "3 sources, 3 pipelines." in page


def test_each_flow_can_be_found_by_what_a_person_would_type(client, source, ckan, _isolated_db):
    _ready(client, source)
    client.post("/load/sources/%s/ckan" % source["id"], data={"mode": "link", "organization_id": "org1", "dataset_id": "ds1", "resource_name": "Traffic"})
    _organization(_isolated_db, source["id"], "org1", "Ministry of Transport")
    page = _page(client)
    attribute = re.search(r'data-flag="\w+" data-text="([^"]*)" id="flow-%s"' % source["id"], page).group(1)
    for word in ("acme push", "acme-push", "ministry of transport", "attica traffic", "default"):
        assert word in attribute


# -- who sees what ---------------------------------------------------------------------------------------------------------------------------------------------------------

def test_an_organization_administrator_sees_only_the_sources_of_their_organizations(client, source, ckan, org_admin, _isolated_db):
    mine = _http_source(_isolated_db, "mine", post=True)
    theirs = _http_source(_isolated_db, "theirs", post=True)
    nobodys = _http_source(_isolated_db, "nobodys", post=True)
    _organization(_isolated_db, mine, "org1", "Traffic")  # the one they administer (the fake CKAN says org1)
    _organization(_isolated_db, theirs, "org2", "Air")  # another organization
    page = _page(client)
    assert 'id="flow-%s"' % mine in page and 'id="flow-%s"' % theirs not in page and 'id="flow-%s"' % nobodys not in page and 'id="flow-%s"' % source["id"] not in page
    assert "The pipelines of the organizations you administer (Traffic)" in page and "1 source, 1 pipeline." in page
    assert "No organization" not in page and "<select" not in page  # nothing to switch between


def test_why_there_is_nothing_to_show_is_said(client, source, ckan, org_admin, monkeypatch, _isolated_db):
    mine = _http_source(_isolated_db, "mine", post=True)
    _organization(_isolated_db, mine, "org1", "Traffic")
    monkeypatch.setattr(ui_provider, "_signed_in", lambda request: (False, ""))
    assert "Sign in with your data.gov.gr account" in _page(client) and 'id="flow-%s"' % mine not in _page(client)
    monkeypatch.setattr(ui_provider, "_signed_in", lambda request: (False, "user_abc"))
    ckan.fail["organization_list_for_user"] = ckan_client.CKANActionError("organization_list_for_user", "down")
    ckan_load._ORG_CACHE.clear()
    assert "could not be asked which organizations you administer" in _page(client)
    ckan_load._ORG_CACHE.clear()
    monkeypatch.delenv("CKAN_BASE_URL")
    assert "CKAN is not set up on this deployment" in _page(client)


def test_someone_who_administers_no_organization_sees_nothing_and_is_told(client, source, ckan, org_admin, monkeypatch):
    monkeypatch.setattr(ckan, "call", lambda action, data, files=None: [] if action == "organization_list_for_user" else None)
    ckan_load._ORG_CACHE.clear()
    page = _page(client)
    assert "You do not administer any organization in CKAN." in page and "Nothing to show." in page


def test_visible_sources_for_a_super_admin_is_everything_without_asking_ckan(_isolated_db, ckan, _master_key):
    _http_source(_isolated_db, "one", post=True)
    with _isolated_db() as session:
        scope = overview.visible_sources(session, is_super=True, ckan_user="")
    assert scope.everything and len(scope.sources) == 1 and ckan.calls == []


def test_the_page_script_draws_the_connectors_and_filters(client, source, ckan):
    page = _page(client)
    for needed in ("function drawFlow(flow)", "function curve(x1, y1, x2, y2)", "createElementNS(SVG, name)", "ResizeObserver", "function apply()",
                   "'live' : 'idle'", "prefers-reduced-motion"):
        assert needed in page
    assert "@keyframes ovflow" in page and ".flow-lines .live" in page
