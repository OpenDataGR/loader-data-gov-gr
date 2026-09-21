"""Envelope-encryption SecretsBackend (MVP).

Each credential is encrypted with a fresh, random data key (DEK); the DEK
itself is then wrapped (encrypted) with a master key (KEK) that lives
outside the CKAN database entirely (an env var / mounted secret file).
Only the wrapped DEK and the DEK-encrypted ciphertext are ever persisted.

Swapping this for a Vault/KMS-backed implementation later only requires a
new class implementing :class:`SecretsBackend` -- callers never construct
this class directly, they go through a factory keyed off this service's
own config.

Ported verbatim from ckanext-providerharvest's ``secrets/envelope.py``
(same crypto, same MVP-vs-Vault tradeoff) -- see that plugin's copy for
the CKAN-coupled provenance of this design before the split.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from providerharvest_service.engine.secrets.base import SecretBundle, SecretsBackend

ALGORITHM = "AES-256-GCM"
_NONCE_LEN = 12  # bytes, standard for AES-GCM


class SecretRepository(Protocol):
    """Storage for encrypted secret rows -- implemented against this
    service's own ``provider_secret`` table (its own SQLAlchemy model,
    not CKAN's -- this service owns its own database now).

    Kept as a narrow protocol (rather than importing the concrete model
    directly) so the crypto logic here is unit-testable with a trivial
    in-memory fake, no database required.
    """

    def create(self, harvest_source_id: str, ciphertext: bytes,
               data_key_wrapped: bytes, algorithm: str) -> str:
        ...

    def get(self, secret_ref: str) -> dict:
        """Return a row dict with at least: ciphertext, data_key_wrapped,
        algorithm, harvest_source_id, version."""

    def update(self, secret_ref: str, ciphertext: bytes,
               data_key_wrapped: bytes) -> None:
        ...

    def delete(self, secret_ref: str) -> None:
        ...


class MasterKeyNotConfigured(RuntimeError):
    pass


def _load_master_key(env_var: str) -> bytes:
    raw = os.environ.get(env_var)
    if not raw:
        raise MasterKeyNotConfigured(
            "%s is not set -- the envelope-encryption master key must be "
            "provisioned outside the CKAN database (env var or mounted "
            "secret file read into this env var at process start)." % env_var
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise MasterKeyNotConfigured(
            "%s is not valid base64" % env_var
        ) from exc
    if len(key) != 32:
        raise MasterKeyNotConfigured(
            "%s must decode to exactly 32 bytes for AES-256-GCM, got %d"
            % (env_var, len(key))
        )
    return key


class EnvelopeSecretsBackend(SecretsBackend):
    """AES-256-GCM envelope encryption, master key outside the CKAN DB."""

    def __init__(self, repository: SecretRepository,
                 master_key_env_var: str = "CKAN_PROVIDERHARVEST_MASTER_KEY"):
        self._repo = repository
        self._master_key_env_var = master_key_env_var

    def _master_key(self) -> bytes:
        # Re-read on every call rather than caching on the instance: lets a
        # rotated master key take effect without restarting long-lived
        # worker processes, and avoids holding key material longer than
        # needed.
        return _load_master_key(self._master_key_env_var)

    def _encrypt(self, fields: dict) -> tuple[bytes, bytes]:
        plaintext = json.dumps(fields).encode("utf-8")

        dek = AESGCM.generate_key(bit_length=256)
        data_nonce = os.urandom(_NONCE_LEN)
        ciphertext = data_nonce + AESGCM(dek).encrypt(data_nonce, plaintext, None)

        kek = self._master_key()
        wrap_nonce = os.urandom(_NONCE_LEN)
        data_key_wrapped = wrap_nonce + AESGCM(kek).encrypt(wrap_nonce, dek, None)

        return ciphertext, data_key_wrapped

    def _decrypt(self, ciphertext: bytes, data_key_wrapped: bytes) -> dict:
        kek = self._master_key()
        wrap_nonce, wrapped = data_key_wrapped[:_NONCE_LEN], data_key_wrapped[_NONCE_LEN:]
        dek = AESGCM(kek).decrypt(wrap_nonce, wrapped, None)

        data_nonce, body = ciphertext[:_NONCE_LEN], ciphertext[_NONCE_LEN:]
        plaintext = AESGCM(dek).decrypt(data_nonce, body, None)
        return json.loads(plaintext.decode("utf-8"))

    def put(self, harvest_source_id: str, fields: dict) -> str:
        ciphertext, data_key_wrapped = self._encrypt(fields)
        return self._repo.create(
            harvest_source_id=harvest_source_id,
            ciphertext=ciphertext,
            data_key_wrapped=data_key_wrapped,
            algorithm=ALGORITHM,
        )

    def get(self, secret_ref: str) -> SecretBundle:
        row = self._repo.get(secret_ref)
        if row["algorithm"] != ALGORITHM:
            raise ValueError(
                "Unsupported secret algorithm %r for %s" % (row["algorithm"], secret_ref)
            )
        fields = self._decrypt(row["ciphertext"], row["data_key_wrapped"])
        return SecretBundle(fields=fields)

    def rotate(self, secret_ref: str, fields: dict) -> str:
        ciphertext, data_key_wrapped = self._encrypt(fields)
        self._repo.update(secret_ref, ciphertext=ciphertext, data_key_wrapped=data_key_wrapped)
        return secret_ref

    def delete(self, secret_ref: str) -> None:
        self._repo.delete(secret_ref)


def new_secret_ref() -> str:
    return str(uuid.uuid4())
