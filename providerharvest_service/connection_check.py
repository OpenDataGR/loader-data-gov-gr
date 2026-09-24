"""Connection health checks: a real dry run against a provider's actual
endpoint, using the already-built and already-tested engine transports
(engine/transport/*) and auth strategies -- confirming reachability and
authentication right now, independent of the registration/approval
`status` machine (a still-pending source can be checked; a checked
source can still be rejected).

This is step 1 of "external connection to the source": it answers
"can we reach and authenticate against this right now", and persists
that answer -- it does NOT fetch or store any actual harvested
records/files. That's the scheduled-harvest slice this sets up for
next (see README's roadmap), which will reuse the exact same
``_build_transport`` dispatch.
"""

from __future__ import annotations

import datetime
import json

from sqlalchemy.orm import Session

from providerharvest_service import provider_sources
from providerharvest_service.engine.auth_strategies import build_auth_strategy
from providerharvest_service.engine.secrets.base import SecretBundle
from providerharvest_service.engine.transport.base import Transport
from providerharvest_service.engine.transport.direct_https import DirectHTTPSTransport
from providerharvest_service.engine.transport.ftp import FTPTransport
from providerharvest_service.engine.transport.scp import ScpTransport
from providerharvest_service.engine.transport.sftp import SFTPTransport
from providerharvest_service.models import ProviderSource


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _build_transport(source: ProviderSource, secret: SecretBundle) -> Transport:
    config = json.loads(source.config or "{}")

    if source.transport_type == "http":
        return DirectHTTPSTransport(
            base_url=source.endpoint_url,
            auth_strategy=build_auth_strategy(source.auth_type),
            secret=secret,
            pagination=config.get("pagination", {}),
            auth_opts=config.get("auth_opts"),
        )

    if source.transport_type in ("sftp", "scp"):
        transport_cls = SFTPTransport if source.transport_type == "sftp" else ScpTransport
        return transport_cls(
            config["host"],
            secret=secret,
            port=config.get("port", 22),
            remote_path=config.get("remote_path", "/"),
            glob_pattern=config.get("glob_pattern", "*"),
            pinned_host_key_fingerprint=config.get("host_key_fingerprint"),
        )

    if source.transport_type == "ftp":
        return FTPTransport(
            config["host"],
            secret=secret,
            port=config.get("port", 21),
            remote_path=config.get("remote_path", "/"),
            glob_pattern=config.get("glob_pattern", "*"),
            use_tls=config.get("use_tls", True),
            plain_ftp_acknowledged=config.get("plain_ftp_acknowledged", False),
        )

    raise ValueError("Unsupported transport_type %r" % source.transport_type)


def check_connection(
    session: Session,
    source_id: str,
    *,
    transport: Transport | None = None,
) -> ProviderSource:
    """Connects, lists one page of entries, and disconnects -- then
    records the result (ok/error, a human-readable message, and the
    timestamp) on the source row regardless of outcome: a failed check
    is exactly as valid a result as a successful one, both get
    persisted, neither raises past this function.

    ``transport`` is accepted purely so tests can inject a fake rather
    than needing a real network target; real callers omit it and
    ``_build_transport`` resolves the right one from the source's own
    config plus its decrypted credentials.
    """
    source = provider_sources.get_or_404(session, source_id)

    status = "ok"
    try:
        if transport is None:
            secret = provider_sources.credentials(session, source_id)
            transport = _build_transport(source, secret)
        with transport:
            page = transport.list_entries(cursor=None)
        count = len(page.entries)
        noun = "record" if source.transport_type == "http" else "file"
        message = "Reachable -- found %d %s%s." % (count, noun, "" if count == 1 else "s")
    except Exception as exc:  # noqa: BLE001 -- shown verbatim, this IS the diagnostic
        status = "error"
        message = str(exc)

    source.last_check_at = _utcnow()
    source.last_check_status = status
    source.last_check_message = message
    session.commit()
    return source
