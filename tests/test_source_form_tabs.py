"""The source form is split into tabs: General, Connection, Request and Limits. Every field keeps its name, and each lives on the tab it belongs to,
with all the limits together."""

import re

from tests.test_transform_ui import (  # noqa: F401  (fixtures shared with the Transform Box tests)
    _isolated_db, _isolated_harvest_db, _master_key, client, source,
)

EXPECTED = {
    "general": ["name", "title", "notification_email"],
    "connection": ["kind-pull", "transport-type-pull", "frequency", "endpoint_url", "api_key", "api_key_prefix", "basic_username", "oauth2_client_id", "mtls_client_cert"],
    "request": ["items_path", "proxy_method", "test_request"],
    "limits": ["max_requests_per_minute", "proxy_rate_limit_per_ip", "request_timeout_s", "max_retries", "max_pages"],
}


def _owner(page, element_id):
    """The tab an element sits on: the nearest data-tab marker before it (tab panels do not nest)."""
    position = page.index('id="%s"' % element_id)
    markers = list(re.finditer(r'data-tab="(\w+)"', page[:position]))
    return markers[-1].group(1)


def test_every_field_sits_on_the_tab_it_belongs_to_and_all_limits_are_together(client):
    page = client.get("/provider/sources/new").text
    for tab, ids in EXPECTED.items():
        for element_id in ids:
            assert _owner(page, element_id) == tab, (element_id, tab)
    for tag in ('<div id="section-push" data-tab="connection">', '<fieldset id="fieldset-ssh" data-tab="connection">', '<fieldset id="fieldset-ftp" data-tab="connection">'):
        assert tag in page  # (the push settings are a push source's Connection, like SFTP and FTP are theirs)


def test_the_tab_bar_names_the_four_tabs_and_the_script_hides_the_ones_a_source_does_not_need(client):
    page = client.get("/provider/sources/new").text
    for name in ("general", "connection", "request", "limits"):
        assert 'data-tab-btn="%s"' % name in page
    assert "function availableTabs()" in page and "return ['general', 'connection'];  // how they send" in page  # push: how they send, and who may send
    assert "['general', 'connection']" in page  # SFTP / FTP: no Request or Limits
    assert 'id="form-tabs" role="tablist" hidden' not in page and "form-tabs').hidden" not in page  # the bar never disappears when the choice changes


def test_an_edited_source_shows_the_same_tabs_with_its_values(client, _isolated_db):
    client.post("/provider/sources/new", data={
        "name": "tabbed", "title": "Tabbed", "kind": "pull", "transport_type": "http", "endpoint_url": "https://registry.example.gr/api/", "frequency": "DAILY",
        "auth_type": "api_key", "api_key": "k", "max_requests_per_minute": "11", "proxy_rate_limit_per_ip": "6", "request_timeout_s": "20",
    })
    source_id = re.search(r"/provider/sources/([^/]+)/edit", client.get("/provider/sources").text).group(1)
    page = client.get("/provider/sources/%s/edit" % source_id).text
    assert _owner(page, "max_requests_per_minute") == "limits" and re.search(r'id="max_requests_per_minute"[^>]*value="11"', page)
    assert re.search(r'id="proxy_rate_limit_per_ip"[^>]*value="6"', page) and re.search(r'id="request_timeout_s"[^>]*value="20"', page)
    assert 'class="limits-block"' in page and "Each call" in page and "Rate limits" in page


def test_a_form_with_an_error_opens_on_the_tab_of_the_first_one(client):
    resp = client.post("/provider/sources/new", data={
        "name": "bad", "title": "Bad", "kind": "pull", "transport_type": "http", "endpoint_url": "https://registry.example.gr/api/", "frequency": "DAILY",
        "auth_type": "api_key", "api_key": "k", "max_requests_per_minute": "0",
    })
    assert resp.status_code == 400 and 'class="error"' in resp.text
    assert "first problem" in resp.text and "first.closest('[data-tab]')" in resp.text  # the script picks the tab of the first message


def test_the_tabs_listen_to_the_source_form_not_to_the_first_form_of_the_page(client):
    page = client.get("/provider/sources/new").text
    assert '<form id="source-form"' in page and "document.getElementById('source-form').addEventListener('change'" in page
    assert "document.querySelector('form')" not in page  # the first <form> of every page is the header's Log out


def test_delivery_connector_endpoint_and_schedule_come_in_that_order_on_the_connection_tab(client):
    page = client.get("/provider/sources/new").text
    positions = [page.index(marker) for marker in ('id="kind-pull"', 'class="connector-grid"', 'id="endpoint_url"', 'id="frequency"', "<label>Authentication</label>")]
    assert positions == sorted(positions)


def test_a_new_source_is_registered_step_by_step_and_an_edit_is_just_saved(client, _isolated_db):
    new_page = client.get("/provider/sources/new").text
    assert "var WIZARD = true;" in new_page and 'id="submit-btn"' in new_page and 'id="submit-label">Register source<' in new_page
    assert "'Next: ' + TAB_LABELS[target]" in new_page and "Still needed here: " in new_page
    for needed in ("endpoint_url", "api_key", "basic_username", "oauth2_token_url", "mtls_client_key", "sftp_username", "host_key_fingerprint", "ftp_password", "ftp_plain_ftp_acknowledged"):
        assert "%s:" % needed in new_page.split("var FIELD_NAMES")[1].split("function requiredFields")[0] or needed == "endpoint_url"  # the hint names what is missing
    client.post("/provider/sources/new", data={
        "name": "wiz", "title": "Wiz", "kind": "pull", "transport_type": "http", "endpoint_url": "https://registry.example.gr/api/", "frequency": "DAILY",
        "auth_type": "api_key", "api_key": "k",
    })
    source_id = re.search(r"/provider/sources/([^/]+)/edit", client.get("/provider/sources").text).group(1)
    edit_page = client.get("/provider/sources/%s/edit" % source_id).text
    assert "var WIZARD = false;" in edit_page and 'id="submit-label">Save changes<' in edit_page
