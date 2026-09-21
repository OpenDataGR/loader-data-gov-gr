"""Phase 1.5 transport: SCP, for whole-file (``delivery_mode=bulk_file``)
provider sources whose SSH server exposes only the ``scp`` command, not
the SFTP subsystem (some legacy/hardened systems restrict to this).

Reuses ``SFTPTransport``'s exact connection/host-key/auth code via
``transport.ssh_common`` -- see that module's docstring -- differing only
in the file-listing/reading mechanics SCP's protocol actually allows:

  * ``list_entries`` has no SCP equivalent of SFTP's ``listdir`` call, so
    it runs a single, fixed/parameterized remote command (``find``) over
    an exec channel instead, output parsed defensively. ``remote_path``
    and ``glob_pattern`` are restricted to a safe character set *and*
    shell-quoted before being embedded -- treat them as untrusted-input-
    adjacent even though they're operator-supplied registration config,
    not raw provider input (see DESIGN.md's Phase 1.5 notes on this).
  * ``open_entry`` has no equivalent of SFTP's streamed file handle --
    SCP is a whole-file push/pull protocol, not a seek/chunk one -- so it
    downloads to a throwaway local temp file (removed the moment the
    caller closes it) and returns that for reading via
    ``transport._local_download.TempFileHandle``, rather than holding
    the whole file in memory at once. That handle must be genuinely
    seekable, not just readable -- CKAN's own resource uploader seeks to
    measure the file's size before copying it, confirmed by a real CI
    failure the first version of this file's own handle class didn't
    implement ``seek()`` at all.
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
import time
from typing import BinaryIO, Callable, Optional

import paramiko
import scp as scp_module

from providerharvest_service.engine.audit import OutboundRequestEvent
from providerharvest_service.engine.secrets.base import SecretBundle
from providerharvest_service.engine.transport._local_download import TempFileHandle
from providerharvest_service.engine.transport.base import Entry, Page, Transport
from providerharvest_service.engine.transport.ssh_common import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT_S,
    HostKeyMismatchError,
    connect_and_authenticate,
    fetch_host_key_fingerprint,
)

__all__ = [
    "HostKeyMismatchError", "fetch_host_key_fingerprint", "ScpTransport",
]

#: Deliberately restrictive -- these are operator-supplied registration
#: fields that end up embedded in a remote shell command (see module
#: docstring). shlex.quote() alone would already make injection
#: impossible, but this rejects anything not shaped like a path/glob
#: before quoting even gets involved, as a second independent layer.
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./\-]+$")
_SAFE_GLOB_RE = re.compile(r"^[A-Za-z0-9_.\-*?\[\]]+$")


def _default_command_runner(transport: "paramiko.Transport", command: str, timeout_s: int) -> bytes:
    channel = transport.open_session()
    try:
        channel.settimeout(timeout_s)
        channel.exec_command(command)
        stdout = channel.makefile("rb").read()
        stderr = channel.makefile_stderr("rb").read()
        exit_status = channel.recv_exit_status()
        if exit_status != 0:
            raise RuntimeError(
                "Remote command failed (exit %d): %s"
                % (exit_status, stderr.decode("utf-8", "replace").strip())
            )
        return stdout
    finally:
        channel.close()


class ScpTransport(Transport):
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
        scp_client_factory: Callable[["paramiko.Transport"], "scp_module.SCPClient"] = (
            scp_module.SCPClient
        ),
        command_runner: Callable[["paramiko.Transport", str, int], bytes] = _default_command_runner,
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
        self._scp_client_factory = scp_client_factory
        self._command_runner = command_runner
        self._transport: Optional["paramiko.Transport"] = None

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
        except Exception as exc:  # noqa: BLE001 -- reported then re-raised, not swallowed
            error = str(exc)
            raise
        finally:
            self._report(
                "CONNECT", self._remote_path,
                status=None, duration_ms=(time.monotonic() - started) * 1000, error=error,
            )

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def list_entries(self, cursor: Optional[str]) -> Page:
        # One page, always -- same reasoning as SFTPTransport: a remote
        # directory listing is bounded, there's no cursor state to carry.
        if cursor is not None:
            return Page(entries=[], next_cursor=None)

        assert self._transport is not None, "connect() must be called before listing"
        if not _SAFE_PATH_RE.match(self._remote_path):
            raise ValueError(
                "remote_path contains characters outside the allowed safe set: %r"
                % self._remote_path
            )
        if not _SAFE_GLOB_RE.match(self._glob_pattern):
            raise ValueError(
                "glob_pattern contains characters outside the allowed safe set: %r"
                % self._glob_pattern
            )

        command = "find %s -mindepth 1 -maxdepth 1 -type f -name %s -printf '%%f\\t%%T@\\t%%s\\n'" % (
            shlex.quote(self._remote_path), shlex.quote(self._glob_pattern),
        )
        started = time.monotonic()
        error = None
        try:
            stdout = self._command_runner(self._transport, command, self._connect_timeout_s)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            raise
        finally:
            self._report(
                "LIST", self._remote_path,
                status=None, duration_ms=(time.monotonic() - started) * 1000, error=error,
            )

        entries = []
        for line in stdout.decode("utf-8", "replace").splitlines():
            # Parsed defensively (per DESIGN.md): silently skip anything
            # that doesn't match the exact shape our own -printf produced
            # rather than guessing at malformed output.
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            filename, mtime_str, size_str = parts
            try:
                mtime = float(mtime_str)
                size = int(size_str)
            except ValueError:
                continue
            entries.append(Entry(
                ref=self._remote_path.rstrip("/") + "/" + filename,
                metadata={"mtime": mtime, "size": size},
            ))
        return Page(entries=entries, next_cursor=None)

    def open_entry(self, entry: Entry) -> BinaryIO:
        assert self._transport is not None, "connect() must be called before opening a file"
        started = time.monotonic()
        error = None
        fd, local_path = tempfile.mkstemp(prefix="providerharvest-scp-")
        os.close(fd)
        try:
            client = self._scp_client_factory(self._transport)
            try:
                client.get(entry.ref, local_path)
            finally:
                close = getattr(client, "close", None)
                if close:
                    close()
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
