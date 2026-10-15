"""Confirmations and prompts are an in-page dialog, not the browser's own box: one dialog in the base page that every page gets, answering the
templates' usual onsubmit="return confirm(...)" and the reject reason prompt."""

from tests.test_transform_ui import _isolated_db, _isolated_harvest_db, _master_key, client, source  # noqa: F401


def test_every_page_carries_the_dialog_and_the_script_that_stands_in_for_confirm_and_prompt(client, source):
    for path in ("/provider/sources", "/transform", "/load", "/provider/sources/new"):
        page = client.get(path).text
        assert 'id="dialog-modal" class="dialog-backdrop" hidden' in page and 'role="dialog" aria-modal="true"' in page, path
        assert "window.confirm = function (message)" in page and "window.prompt = function (message, defaultValue)" in page, path


def test_the_login_page_has_it_too_and_the_templates_still_ask_the_usual_way(client, source):
    from fastapi.testclient import TestClient

    from providerharvest_service.app import app

    login = TestClient(app, base_url="https://testserver").get("/admin/login").text
    assert 'id="dialog-modal"' in login
    page = client.get("/provider/sources").text
    assert "return confirm('This makes one real call" in page or "return confirm('Permanently delete" in page  # (no template had to change)


def test_a_destructive_question_is_styled_as_one_and_the_dialog_can_be_dismissed_by_keyboard(client):
    page = client.get("/provider/sources").text
    assert "#dialog-ok.danger { background: var(--status-rejected); }" in page
    assert "delete|remove|permanently|stop |replace|new api key|cannot be undone" in page
    assert "e.key === 'Escape'" in page and "e.key === 'Enter' && e.target === input" in page and "e.key === 'Tab'" in page
