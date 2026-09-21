"""Phase 1.5 transport: FTP/FTPS, for whole-file (``delivery_mode=bulk_file``)
provider sources whose only transfer option is legacy (S)FTP, not
SFTP-over-SSH.

Security properties this transport is responsible for maintaining:
  * **Defaults to FTPS** (explicit AUTH TLS, ``ftplib.FTP_TLS``) with a
    real, verifying TLS context (``ssl.create_default_context()``,
    *never* relying on ftplib's own default context or disabling
    verification) -- the FTP analogue of ``DirectHTTPSTransport``'s
    ``verify=True`` default. Both the control connection and the data
    connection (file transfers) are secured -- see ``prot_p()`` below;
    securing only the control channel would leave file contents and the
    password sent unencrypted over the data channel.
  * Plain, unencrypted FTP sends credentials and file contents in the
    clear. It is only permitted when the source was registered with an
    explicit, individually-flagged acknowledgment
    (``ProviderSourceExtension.plain_ftp_acknowledged`` -- see
    ``provider_source_create``): ``connect()`` refuses outright if
    ``use_tls`` is False and that flag isn't True. There is no silent
    fallback from FTPS to plain FTP.
  * The network target is (re-)validated via ``logic.validators`` on
    every ``connect()``, same requirement as every other transport.
  * ``list_entries`` uses MLSD (RFC 3659's structured directory listing,
    supported by every modern FTP server *except* vsftpd, notably --
    confirmed the hard way via a real CI run against a vsftpd test
    server, which rejected it outright with "500 Unknown command"; the
    Docker replica's own ftp-provider container now runs ProFTPD
    instead) rather than parsing legacy ``LIST`` output, avoiding a
    whole class of format-parsing bugs plain-text directory listings are
    prone to.
  * ``open_entry`` downloads to a throwaway local temp file (removed the
    moment the caller closes it, via
    ``transport._local_download.TempFileHandle``) rather than streaming
    the raw FTP data-connection socket directly. A raw socket was tried
    first, but CKAN's own resource uploader seeks on the handle it's
    given (to measure the file size before copying) -- a socket
    fundamentally can't support that, confirmed by a real CI failure.
"""

from __future__ import annotations

import fnmatch
import ftplib
import os
import ssl
import tempfile
import time
from typing import BinaryIO, Callable, Optional

from providerharvest_service.engine.audit import OutboundRequestEvent
from providerharvest_service.engine.validators import assert_safe_network_target
from providerharvest_service.engine.secrets.base import SecretBundle
from providerharvest_service.engine.transport._local_download import TempFileHandle
from providerharvest_service.engine.transport.base import Entry, Page, Transport

DEFAULT_PORT = 21
DEFAULT_TIMEOUT_S = 30


class PlainFtpNotAcknowledgedError(Exception):
    """``use_tls=False`` was requested but this source was never
    registered with the explicit plain-FTP acknowledgment -- see the
    module docstring. Refuses rather than silently connecting insecurely."""


class FTPTransport(Transport):
    def __init__(
        self,
        host: str,
        secret: SecretBundle,
        *,
        port: int = DEFAULT_PORT,
        remote_path: str = "/",
        glob_pattern: str = "*",
        use_tls: bool = True,
        plain_ftp_acknowledged: bool = False,
        allow_private_ranges: bool = False,
        connect_timeout_s: int = DEFAULT_TIMEOUT_S,
        on_request: Optional[Callable[[OutboundRequestEvent], None]] = None,
        harvest_source_id: Optional[str] = None,
        harvest_job_id: Optional[str] = None,
        client_factory: Optional[Callable[[], "ftplib.FTP"]] = None,
    ):
        self._host = host
        self._port = port
        self._secret = secret
        self._remote_path = remote_path
        self._glob_pattern = glob_pattern
        self._use_tls = use_tls
        self._plain_ftp_acknowledged = plain_ftp_acknowledged
        self._allow_private_ranges = allow_private_ranges
        self._connect_timeout_s = connect_timeout_s
        self._on_request = on_request
        self._harvest_source_id = harvest_source_id
        self._harvest_job_id = harvest_job_id
        self._client_factory = client_factory
        self._client: Optional["ftplib.FTP"] = None

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
            auth_type_used="ftps_password" if self._use_tls else "ftp_password",
            error=error,
        ))

    def _default_client(self) -> "ftplib.FTP":
        if self._use_tls:
            return ftplib.FTP_TLS(context=ssl.create_default_context())
        return ftplib.FTP()

    def connect(self) -> None:
        started = time.monotonic()
        error = None
        try:
            assert_safe_network_target(
                self._host, self._port, allow_private_ranges=self._allow_private_ranges
            )
            if not self._use_tls and not self._plain_ftp_acknowledged:
                raise PlainFtpNotAcknowledgedError(
                    "Plain (unencrypted) FTP requires this source to have been "
                    "registered with an explicit plain-FTP acknowledgment -- see "
                    "DESIGN.md's Phase 1.5 notes. Use FTPS (use_tls=True) instead "
                    "unless this provider genuinely has no FTPS capability."
                )

            client = self._client_factory() if self._client_factory else self._default_client()
            client.connect(self._host, self._port, timeout=self._connect_timeout_s)

            fields = self._secret.fields
            username = fields.get("username")
            if not username:
                raise ValueError("FTP secret bundle is missing 'username'")
            client.login(username, fields.get("password", ""))

            if self._use_tls:
                # Secures the data channel too, not just the control
                # connection -- without this, file contents (and, for
                # some servers, credentials) still cross the data
                # connection in the clear even though login() itself was
                # encrypted.
                client.prot_p()

            self._client = client
        except Exception as exc:  # noqa: BLE001 -- reported then re-raised, not swallowed
            error = str(exc)
            raise
        finally:
            self._report(
                "CONNECT", self._remote_path,
                status=None, duration_ms=(time.monotonic() - started) * 1000, error=error,
            )

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.quit()
            except Exception:  # noqa: BLE001 -- best-effort graceful close
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass
            self._client = None

    def list_entries(self, cursor: Optional[str]) -> Page:
        # One page, always -- same reasoning as SFTPTransport/ScpTransport:
        # a remote directory listing is bounded, there's no cursor state.
        if cursor is not None:
            return Page(entries=[], next_cursor=None)

        assert self._client is not None, "connect() must be called before listing"
        started = time.monotonic()
        error = None
        try:
            listing = list(self._client.mlsd(self._remote_path))
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            raise
        finally:
            self._report(
                "LIST", self._remote_path,
                status=None, duration_ms=(time.monotonic() - started) * 1000, error=error,
            )

        entries = []
        for filename, facts in listing:
            if facts.get("type") != "file":
                continue
            if not fnmatch.fnmatch(filename, self._glob_pattern):
                continue
            size = facts.get("size")
            entries.append(Entry(
                ref=self._remote_path.rstrip("/") + "/" + filename,
                metadata={
                    "size": int(size) if size is not None else None,
                    "modify": facts.get("modify"),  # RFC 3659: YYYYMMDDHHMMSS[.sss]
                },
            ))
        return Page(entries=entries, next_cursor=None)

    def open_entry(self, entry: Entry) -> BinaryIO:
        assert self._client is not None, "connect() must be called before opening a file"
        started = time.monotonic()
        error = None
        fd, local_path = tempfile.mkstemp(prefix="providerharvest-ftp-")
        os.close(fd)
        try:
            with open(local_path, "wb") as fh:
                # retrbinary handles TYPE I (binary mode) and finalizing
                # the transfer/control-connection response itself.
                self._client.retrbinary("RETR " + entry.ref, fh.write)
            return TempFileHandle(local_path)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            try:
                os.remove(local_path)
            except OSError:
                pass
            raise
        finally:
            self._report(
                "GET", entry.ref,
                status=None, duration_ms=(time.monotonic() - started) * 1000, error=error,
            )
