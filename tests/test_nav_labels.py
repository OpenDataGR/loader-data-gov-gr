"""The masthead's tabs follow the ETL flow (see the README): the provider-facing pages are the Extract Box."""

from tests.test_ui_admin_ftp import (  # noqa: F401  (fixtures shared with the admin page tests)
    _admin_session_env, _isolated_db, _master_key, _upload_root, client,
)


def test_the_provider_pages_tab_is_the_extract_box_and_the_old_name_is_gone(client):
    for path in ("/provider/sources", "/admin/sources", "/admin/ftp"):
        page = client.get(path).text
        assert "Extract Box</a>" in page, path
        assert "Provider portal</a>" not in page, path  # (the Changelog may still *mention* the old name in a commit title)


def test_the_tab_still_points_at_the_same_address_and_is_marked_active_there(client):
    page = client.get("/provider/sources").text
    assert 'href="/provider/sources" class="active"' in page
