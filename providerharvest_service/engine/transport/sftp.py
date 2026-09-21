"""Phase 1.5 transport: SFTP, for whole-file (``delivery_mode=bulk_file``)
provider sources.

Security properties this transport is responsible for maintaining:
  * The network target (host:port) is (re-)validated via
    ``logic.validators`` on every ``connect()`` -- same requirement and
    same reasoning as the HTTP transport.
  * The server's host key is pinned at registration time (see
    ``fetch_host_key_fingerprint``, used by the
    ``provider_source_fetch_host_key`` action) and verified on every
    connection: SSH has no CA hierarchy, so trust-on-first-use-with-
    pinning is the standard safe pattern -- the SSH analogue of TLS
    chain validation. There is deliberately no "skip host key check"
    option; a mismatch always raises. This (and the authentication step)
    is shared with ``ScpTransport`` via ``transport.ssh_common`` -- same
    SSH connection, same trust model, only the file-listing/reading
    mechanics differ between the two subsystems.
  * Files are listed (path/mtime/size only, never bytes) and opened as
    streamed, seekable-free handles -- callers must not read a whole
    file into memory, since these can be multi-GB provider exports.
  * Every connection attempt is reported via ``on_request`` for audit
    logging, reusing the same ``OutboundRequestEvent`` shape the HTTP
    transport uses -- never credentials.
"""

from __future__ import annotations

import fnmatch
import stat
import time
from typing import BinaryIO, Callable, Optional

import paramiko

from providerharvest_service.engine.audit import OutboundRequestEvent
from providerharvest_service.engine.secrets.base import SecretBundle
from providerharvest_service.engine.transport.base import Entry, Page, Transport
from providerharvest_service.engine.transport.ssh_common import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT_S,
    HostKeyMismatchError,
    connect_and_authenticate,
    fetch_host_key_fingerprint,
)

__all__ = [
    "HostKeyMismatchError", "fetch_host_key_fingerprint", "SFTPTransport",
]


class SFTPTransport(Transport):
    def __init__(
        self,
        host: str,
        secret: SecretBundle,
        *,
        port: int = DEFAULT_PORT,
        remote_path: str = "/",
        glob_pattern: str = "*",
        pinned_host_key_fingerprint: Optional[str] = None,
        allow_private_ranges: bool = False,
        connect_timeout_s: int = DEFAULT_TIMEOUT_S,
        on_request: Optional[Callable[[OutboundRequestEvent], None]] = None,
        harvest_source_id: Optional[str] = None,
        harvest_job_id: Optional[str] = None,
        transport_factory: Callable[[tuple], "paramiko.Transport"] = paramiko.Transport,
        sftp_client_factory: Callable[["paramiko.Transport"], "paramiko.SFTPClient"] = (
            paramiko.SFTPClient.from_transport
        ),
    ):
        self._host = host
        self._port = port
        self._secret = secret
        self._remote_path = remote_path
        self._glob_pattern = glob_pattern
        self._pinned_host_key_fingerprint = pinned_host_key_fingerprint
        self._allow_private_ranges = allow_private_ranges
        self._connect_timeout_s = connect_timeout_s
        self._on_request = on_request
        self._harvest_source_id = harvest_source_id
        self._harvest_job_id = harvest_job_id
        self._transport_factory = transport_factory
        self._sftp_client_factory = sftp_client_factory
        self._transport: Optional["paramiko.Transport"] = None
        self._sftp: Optional["paramiko.SFTPClient"] = None

    def _report(self, method: str, path: str, *, status: Optional[int],
                duration_ms: float, error: Optional[str] = None) -> None:
        if self._on_request is None:
            return
        self._on_request(OutboundRequestEvent(
            harvest_source_id=self._harvest_source_id,
            harvest_job_id=self._harvest_job_id,
            host=self._host,
            path=path,
            method=method,
            status=status,
            duration_ms=duration_ms,
            auth_type_used="ssh_key" if "private_key_pem" in self._secret.fields else "ssh_password",
            error=error,
        ))

    def connect(self) -> None:
        started = time.monotonic()
        error = None
        try:
            self._transport = connect_and_authenticate(
                self._host, self._port, self._secret,
                pinned_host_key_fingerprint=self._pinned_host_key_fingerprint,
                allow_private_ranges=self._allow_private_ranges,
                connect_timeout_s=self._connect_timeout_s,
                transport_factory=self._transport_factory,
            )
            self._sftp = self._sftp_client_factory(self._transport)
        except Exception as exc:  # noqa: BLE001 -- reported then re-raised, not swallowed
            error = str(exc)
            raise
        finally:
            self._report(
                "CONNECT", self._remote_path,
                status=None, duration_ms=(time.monotonic() - started) * 1000, error=error,
            )

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
            self._sftp = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def list_entries(self, cursor: Optional[str]) -> Page:
        # One page, always: a remote directory listing is bounded (unlike
        # an HTTP API that could paginate over an unbounded record set),
        # so there's no cursor state to carry between calls.
        if cursor is not None:
            return Page(entries=[], next_cursor=None)

        assert self._sftp is not None, "connect() must be called before listing"
        started = time.monotonic()
        error = None
        try:
            attrs = self._sftp.listdir_attr(self._remote_path)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            raise
        finally:
            self._report(
                "LIST", self._remote_path,
                status=None, duration_ms=(time.monotonic() - started) * 1000, error=error,
            )

        entries = [
            Entry(
                ref=self._remote_path.rstrip("/") + "/" + a.filename,
                metadata={"mtime": a.st_mtime, "size": a.st_size},
            )
            for a in attrs
            if not stat.S_ISDIR(a.st_mode or 0)
            and fnmatch.fnmatch(a.filename, self._glob_pattern)
        ]
        return Page(entries=entries, next_cursor=None)

    def open_entry(self, entry: Entry) -> BinaryIO:
        assert self._sftp is not None, "connect() must be called before opening a file"
        started = time.monotonic()
        error = None
        try:
            handle = self._sftp.open(entry.ref, "rb")
            # Buffered, chunked reads instead of paramiko's default
            # request-per-read-call behaviour -- meaningfully faster for
            # large files without ever holding a whole file in memory.
            handle.prefetch()
            return handle
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            raise
        finally:
            self._report(
                "GET", entry.ref,
                status=None, duration_ms=(time.monotonic() - started) * 1000, error=error,
            )
