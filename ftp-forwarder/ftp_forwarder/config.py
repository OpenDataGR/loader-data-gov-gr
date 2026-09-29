"""The forwarder's own tunable settings -- not the account list, which
lives in this box's local ProFTPD account database and is discovered
via ``accounts.py`` instead (see that module's own docstring for why:
loader's admin panel is the single place accounts are created, edited,
or disabled, so a separate hand-maintained file here would just be a
second, driftable copy of the same list).
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class ForwarderConfig:
    loader_base_url: str
    # The same LOADER_FTP_SYNC_KEY account_sync.py already uses to pull
    # the account list -- reused here rather than a separate per-source
    # API key, since loader's own ftp_relay_ingest.py authenticates
    # this box as itself and looks up the per-account forwarding target
    # server-side (see that module's docstring).
    sync_key: str
    archive_subdir: str = ".forwarded"
    max_retries: int = 5
    retry_backoff_s: float = 1.0
    worker_count: int = 8
    catch_up_interval_s: int = 60
