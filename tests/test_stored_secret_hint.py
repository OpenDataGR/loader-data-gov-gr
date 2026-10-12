"""Editing a source never sends a stored secret back to the browser -- so the empty field has to say that something is
there: dots in the field and a "stored" badge. A new source (nothing stored) shows neither."""

import re

from tests.test_ui_provider import (  # noqa: F401  (fixtures and helpers shared with the provider UI tests)
    _admin_session_env, _isolated_db, _isolated_harvest_db, _master_key, _register_and_get_id, _valid_form, client,
)

DOTS = "•" * 12


def _edit_page(client, source_id):
    resp = client.get("/provider/sources/%s/edit" % source_id)
    assert resp.status_code == 200
    return resp.text


def _field(page, field_id):
    """The <input> or <textarea> opening tag with this id."""
    return re.search(r'<(?:input|textarea)[^>]*id="%s"[^>]*>' % field_id, page).group(0)


def _label(page, field_id):
    return re.search(r'<label for="%s">.*?</label>' % field_id, page, re.S).group(0)


def test_a_stored_api_key_is_shown_as_dots_and_a_stored_badge_without_revealing_it(client):
    source_id = _register_and_get_id(client)  # registered with api_key "secret-key-123"
    page = _edit_page(client, source_id)
    assert 'placeholder="%s"' % DOTS in _field(page, "api_key") and "stored" in _label(page, "api_key")
    assert "secret-key-123" not in page
    assert 'value=""' in _field(page, "api_key")  # still empty: leaving it empty keeps the stored key


def test_a_new_source_shows_no_dots_and_no_badge(client):
    page = client.get("/provider/sources/new").text
    assert DOTS not in _field(page, "api_key") and "stored" not in _label(page, "api_key")


def test_only_the_credentials_that_are_really_stored_show_dots(client):
    client.post("/provider/sources/new", data=_valid_form(auth_type="basic_auth", basic_username="u", basic_password="p", api_key=""))
    source_id = re.search(r"/provider/sources/([^/]+)/check-connection", client.get("/provider/sources").text).group(1)
    page = _edit_page(client, source_id)
    for field in ("basic_username", "basic_password"):
        assert DOTS in _field(page, field) and "stored" in _label(page, field)
    assert DOTS not in _field(page, "api_key") and "stored" not in _label(page, "api_key")  # no key is stored for this source
    assert DOTS not in _field(page, "oauth2_client_secret")


def test_the_word_dropdown_is_in_the_key_row_and_preselects_what_is_stored(client):
    client.post("/provider/sources/new", data=_valid_form(name="keyrow-test", api_key_prefix="Bearer"))
    source_id = re.search(r"/provider/sources/([^/]+)/check-connection", client.get("/provider/sources").text).group(1)
    page = client.get("/provider/sources/%s/edit" % source_id).text
    row = re.search(r'<div class="key-row".*?</div>', page, re.S).group(0)
    assert row.index('id="api_key_prefix"') < row.index('id="api_key_prefix_custom"') < row.index('id="api_key"')  # word, custom box, key
    assert '<option value="Bearer" selected>' in row and "display:none" in row  # the custom box stays hidden unless chosen
