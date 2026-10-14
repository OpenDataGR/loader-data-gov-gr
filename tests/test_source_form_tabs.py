"""The source form is split into tabs: General, Connection, Request and Limits. Every field keeps its name, and each lives on the tab it belongs to,
with all the limits together."""

import re

from tests.test_transform_ui import (  # noqa: F401  (fixtures shared with the Transform Box tests)
    _isolated_db, _isolated_harvest_db, _master_key, client, source,
)

EXPECTED = {
    "general": ["name", "title", "notification_email", "kind-pull", "frequency", "transport-type-pull"],
    "connection": ["endpoint_url", "api_key", "api_key_prefix", "basic_username", "oauth2_client_id", "mtls_client_cert"],
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
    for tag in ('<div id="section-push" data-tab="general">', '<fieldset id="fieldset-ssh" data-tab="connection">', '<fieldset id="fieldset-ftp" data-tab="connection">'):
        assert tag in page  # (the push settings are the whole of a push source's form; SFTP and FTP are their Connection)


def test_the_tab_bar_names_the_four_tabs_and_the_script_hides_the_ones_a_source_does_not_need(client):
    page = client.get("/provider/sources/new").text
    for name in ("general", "connection", "request", "limits"):
        assert 'data-tab-btn="%s"' % name in page
    assert "function availableTabs()" in page and "return ['general']" in page  # a push source has only the General tab
    assert "['general', 'connection']" in page  # SFTP / FTP: no Request or Limits


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
