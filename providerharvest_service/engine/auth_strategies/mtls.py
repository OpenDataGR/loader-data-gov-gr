"""Mutual TLS (client certificate) authentication -- the client (this
harvester) presents a certificate as part of the TLS handshake itself,
not any application-layer header or param. Unlike every other
AuthStrategy, there's nothing to add to headers/params; instead this
sets ``cert`` in the returned request kwargs, which ``requests``/urllib3
require as a pair of real file paths -- not in-memory PEM data, which is
all a resolved SecretBundle ever holds (matching every other credential
shape in this extension, e.g. SFTP's private_key_pem).

Those temp files hold key material and must not outlive the connection
they were written for: written 0600, and removed by close(), which the
owning transport must call when it's done (see DirectHTTPSTransport.close()).
There's no "proceed without a client cert" fallback -- apply() raises if
the secret doesn't have both a cert and a key.
"""

from __future__ import annotations

import os
import stat
import tempfile
from typing import Optional, Tuple

from providerharvest_service.engine.auth_strategies.base import AuthStrategy
from providerharvest_service.engine.secrets.base import SecretBundle


class MTLSAuth(AuthStrategy):
    """``credential_fields`` (secret): client_cert_pem, client_key_pem."""

    name = "mtls"

    def __init__(self):
        self._cert_path: Optional[str] = None
        self._key_path: Optional[str] = None

    def apply(self, request_kwargs: dict, secret: SecretBundle, auth_opts: dict) -> dict:
        if self._cert_path is None:
            self._cert_path, self._key_path = self._write_temp_files(secret)
        kwargs = dict(request_kwargs)
        kwargs["cert"] = (self._cert_path, self._key_path)
        return kwargs

    def _write_temp_files(self, secret: SecretBundle) -> Tuple[str, str]:
        cert_pem = secret.fields.get("client_cert_pem")
        key_pem = secret.fields.get("client_key_pem")
        if not cert_pem or not key_pem:
            raise ValueError(
                "Secret bundle for mtls auth is missing 'client_cert_pem' "
                "and/or 'client_key_pem'"
            )
        cert_path = self._write_secure_temp_file(cert_pem, suffix=".pem")
        key_path = self._write_secure_temp_file(key_pem, suffix=".key")
        return cert_path, key_path

    @staticmethod
    def _write_secure_temp_file(content: str, *, suffix: str) -> str:
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="providerharvest-mtls-")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 -- key material
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        return path

    def close(self) -> None:
        for path in (self._cert_path, self._key_path):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._cert_path = None
        self._key_path = None
