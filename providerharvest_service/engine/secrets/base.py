"""Abstract interface for storing provider credentials at rest.

HarvestSource.config must never hold a raw credential -- only an opaque
``secret_ref`` handle returned by :meth:`SecretsBackend.put`. The concrete
backend (envelope encryption for MVP, Vault/KMS later) is resolved via CKAN
config so swapping backends is a deployment change, not a code change.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class SecretBundle:
    """A resolved, in-memory-only credential.

    ``fields`` holds whatever shape the auth strategy needs, e.g.
    ``{"api_key": "..."}``, ``{"username": "...", "password": "..."}``, or
    ``{"private_key_pem": "..."}``. Never persisted, never logged, never
    repr'd with real values (see ``__repr__`` override below).
    """

    fields: dict = field(default_factory=dict)

    def __repr__(self) -> str:  # pragma: no cover - defensive only
        return "SecretBundle(fields=<redacted>)"


class SecretsBackend(abc.ABC):
    """Put/get/rotate/delete a provider credential, keyed by an opaque ref."""

    @abc.abstractmethod
    def put(self, harvest_source_id: str, fields: dict) -> str:
        """Store ``fields`` and return an opaque ``secret_ref`` to persist
        in ``HarvestSource.config`` in place of the real credential."""

    @abc.abstractmethod
    def get(self, secret_ref: str) -> SecretBundle:
        """Resolve a previously stored secret. Called only at harvest run
        time; callers must not cache the result beyond the current job."""

    @abc.abstractmethod
    def rotate(self, secret_ref: str, fields: dict) -> str:
        """Replace the stored credential under (ideally) the same ref."""

    @abc.abstractmethod
    def delete(self, secret_ref: str) -> None:
        """Permanently remove a stored credential."""
