import base64
import os

import pytest

from providerharvest_service.engine.secrets.envelope import (
    EnvelopeSecretsBackend,
    MasterKeyNotConfigured,
    new_secret_ref,
)

ENV_VAR = "TEST_PROVIDERHARVEST_MASTER_KEY"


class FakeSecretRepository:
    def __init__(self):
        self._rows: dict[str, dict] = {}

    def create(self, harvest_source_id, ciphertext, data_key_wrapped, algorithm):
        ref = new_secret_ref()
        self._rows[ref] = {
            "ciphertext": ciphertext,
            "data_key_wrapped": data_key_wrapped,
            "algorithm": algorithm,
            "harvest_source_id": harvest_source_id,
            "version": 1,
        }
        return ref

    def get(self, secret_ref):
        return dict(self._rows[secret_ref])

    def update(self, secret_ref, ciphertext, data_key_wrapped):
        row = self._rows[secret_ref]
        row["ciphertext"] = ciphertext
        row["data_key_wrapped"] = data_key_wrapped
        row["version"] += 1

    def delete(self, secret_ref):
        del self._rows[secret_ref]


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv(ENV_VAR, key)
    yield key


@pytest.fixture
def backend():
    return EnvelopeSecretsBackend(repository=FakeSecretRepository(), master_key_env_var=ENV_VAR)


def test_round_trip(backend):
    ref = backend.put("source-1", {"api_key": "s3cr3t-value"})
    bundle = backend.get(ref)
    assert bundle.fields == {"api_key": "s3cr3t-value"}


def test_ciphertext_never_contains_plaintext(backend):
    repo = backend._repo
    ref = backend.put("source-1", {"api_key": "very-unique-plaintext-marker"})
    row = repo.get(ref)
    assert b"very-unique-plaintext-marker" not in row["ciphertext"]
    assert b"very-unique-plaintext-marker" not in row["data_key_wrapped"]


def test_rotate_changes_ciphertext_and_still_decrypts(backend):
    ref = backend.put("source-1", {"api_key": "old-value"})
    before = backend._repo.get(ref)["ciphertext"]

    backend.rotate(ref, {"api_key": "new-value"})
    after = backend._repo.get(ref)["ciphertext"]

    assert before != after
    assert backend.get(ref).fields == {"api_key": "new-value"}


def test_delete_removes_secret(backend):
    ref = backend.put("source-1", {"api_key": "value"})
    backend.delete(ref)
    with pytest.raises(KeyError):
        backend.get(ref)


def test_missing_master_key_raises(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    backend = EnvelopeSecretsBackend(repository=FakeSecretRepository(), master_key_env_var=ENV_VAR)
    with pytest.raises(MasterKeyNotConfigured):
        backend.put("source-1", {"api_key": "value"})


def test_wrong_length_master_key_raises(monkeypatch):
    monkeypatch.setenv(ENV_VAR, base64.b64encode(b"short").decode())
    backend = EnvelopeSecretsBackend(repository=FakeSecretRepository(), master_key_env_var=ENV_VAR)
    with pytest.raises(MasterKeyNotConfigured):
        backend.put("source-1", {"api_key": "value"})


def test_secret_bundle_repr_never_leaks_values(backend):
    ref = backend.put("source-1", {"api_key": "top-secret-marker"})
    bundle = backend.get(ref)
    assert "top-secret-marker" not in repr(bundle)
