"""The word some APIs want before the key (``Authorization: Token <key>``) is its own field, so the key is entered alone."""

import pytest

from providerharvest_service import provider_sources, proxy
from providerharvest_service.engine.auth_strategies.api_key import ApiKeyAuth
from providerharvest_service.engine.secrets.base import SecretBundle
from tests.test_proxy import (  # noqa: F401  (fixtures and helpers shared with the proxy tests)
    FakeUpstreamResponse, _clear_rate_limiters, _isolated_db, _master_key, client,
)


def _apply(key, **opts):
    return ApiKeyAuth().apply({}, SecretBundle(fields={"api_key": key}), {"name": "Authorization", **opts})["headers"]["Authorization"]


def test_the_word_is_put_before_the_key_in_a_header():
    assert _apply("abc123", prefix="Token") == "Token abc123"
    assert _apply("abc123", prefix="Bearer") == "Bearer abc123"


def test_without_a_word_the_key_is_sent_alone_as_before():
    assert _apply("abc123") == "abc123" and _apply("abc123", prefix="") == "abc123" and _apply("abc123", prefix="  ") == "abc123"


def test_a_key_that_already_starts_with_the_word_is_not_given_it_twice():
    assert _apply("Token abc123", prefix="Token") == "Token abc123"
    assert _apply("token abc123", prefix="Token") == "token abc123"
    assert _apply("Tokenabc123", prefix="Token") == "Token Tokenabc123"  # only a whole word counts


def test_the_word_is_for_headers_only_not_for_a_query_parameter():
    kwargs = ApiKeyAuth().apply({}, SecretBundle(fields={"api_key": "abc123"}), {"location": "query", "name": "k", "prefix": "Token"})
    assert kwargs["params"] == {"k": "abc123"}


def _form(**extra):
    return {"name": "registry", "title": "Registry", "transport_type": "http", "endpoint_url": "https://registry.example.gr/api/",
            "auth_type": "api_key", "api_key": "abc123", "api_key_location": "header", "api_key_name": "Authorization", **extra}


def test_the_word_is_kept_on_the_source_validated_and_shown_again_when_editing(_isolated_db):
    with _isolated_db() as session:
        source = provider_sources.register(session, _form(api_key_prefix="Token"))
        source_id = source.id
    with _isolated_db() as session:
        source = provider_sources.get_or_404(session, source_id)
        assert '"prefix": "Token"' in source.config
        for bad in ("two words", "Tok;en", "1abc", "x" * 30):
            with pytest.raises(provider_sources.ValidationError) as excinfo:
                provider_sources.register(session, _form(name="other", api_key_prefix=bad))
            assert "api_key_prefix" in excinfo.value.errors


def test_the_proxy_sends_the_word_and_the_key_together(client, _isolated_db, monkeypatch):
    with _isolated_db() as session:
        source = provider_sources.register(session, _form(proxy_method="POST", api_key_prefix="Token", proxy_enabled="on"))
        provider_sources.activate(session, source.id)
    sent = []
    monkeypatch.setattr(proxy.requests, "post", lambda url, **kw: (sent.append(kw), FakeUpstreamResponse())[-1])
    monkeypatch.setattr(proxy, "assert_safe_http_url", lambda url: None)
    client.post("/proxy/registry", content="{}", headers={"Content-Type": "application/json"})
    assert sent[0]["headers"]["Authorization"] == "Token abc123"


def test_the_form_has_the_field_and_explains_it():
    import pathlib

    template = pathlib.Path("providerharvest_service/templates/provider/source_form.html").read_text(encoding="utf-8")
    assert 'name="api_key_prefix"' in template and "Word before the key" in template and "Token" in template


def test_a_custom_word_is_typed_in_its_own_box_and_must_not_be_empty(_isolated_db):
    with _isolated_db() as session:
        source = provider_sources.register(session, _form(api_key_prefix="__custom__", api_key_prefix_custom="Splunk"))
        assert '"prefix": "Splunk"' in source.config
        with pytest.raises(provider_sources.ValidationError) as excinfo:
            provider_sources.register(session, _form(name="other", api_key_prefix="__custom__", api_key_prefix_custom=" "))
        assert "type the word" in excinfo.value.errors["api_key_prefix"]
        with pytest.raises(provider_sources.ValidationError) as excinfo:
            provider_sources.register(session, _form(name="another", api_key_prefix="__custom__", api_key_prefix_custom="two words"))
        assert "api_key_prefix" in excinfo.value.errors
