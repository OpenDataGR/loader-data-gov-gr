"""How long each source's data is kept: on the loader, and (for FTP push sources) on the FTP box.

Two settings per source, both a fixed list of choices -- never free typing -- and both stored in the
source's own ``config`` (``retention_loader_days``, ``retention_box_days``); ``0`` means forever and a
setting that was never made means the default:

* **loader**: how long a received file's *contents* stay in the harvest database (default 90 days).
  When they pass it the contents are emptied but the run, the file's name, type, size and time stay, so
  the history and its counts remain complete; the Runs page then shows the file as removed.
* **box**: how long the FTP box keeps its archive of forwarded files for that source's account
  (default 30 days). Loader only *says* the number (in the account sync); the box deletes, and checks
  it itself (``archive_cleanup.py`` in ftp-forwarder).

Only whole files are purged here. A record stored from an HTTP source is small JSON and is kept.

Shortening a period deletes data that cannot come back, so the admin page previews what would go and
asks first (``admin/source_retention.html``). The purge itself runs hourly in a small thread in each
loader worker -- every pass is idempotent, so several workers doing it at once is harmless.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from typing import Callable

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from providerharvest_service.harvest_models import HarvestedFile, HarvestedRecord, HarvestRun, TransformedFile
from providerharvest_service.models import FtpAccount, ProviderSource

logger = logging.getLogger(__name__)

LOADER_OPTIONS = (7, 30, 90, 180, 365, 0)
BOX_OPTIONS = (1, 7, 14, 30, 90, 0)
LOADER_DEFAULT = 90
BOX_DEFAULT = 30
KEYS = ("retention_loader_days", "retention_box_days")


class RetentionError(ValueError):
    """Carries a message fit to show the admin."""


def label(days: int) -> str:
    return "forever" if days == 0 else ("1 day" if days == 1 else "%d days" % days)


def _config(source: ProviderSource) -> dict:
    try:
        data = json.loads(source.config or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _value(config: dict, key: str, options: tuple[int, ...], default: int) -> int:
    raw = config.get(key)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw in options else default


def loader_days(source: ProviderSource) -> int:
    return _value(_config(source), "retention_loader_days", LOADER_OPTIONS, LOADER_DEFAULT)


def box_days(source: ProviderSource) -> int:
    return _value(_config(source), "retention_box_days", BOX_OPTIONS, BOX_DEFAULT)


def box_days_for_account(session: Session, account: FtpAccount) -> int:
    """What the FTP box is told for this account: its source's setting, or the default for an
    account with no source (one an admin made by hand)."""
    if account.managed_source_id:
        source = session.get(ProviderSource, account.managed_source_id)
        if source is not None:
            return box_days(source)
    return BOX_DEFAULT


def applies_to_box(source: ProviderSource) -> bool:
    """Only an FTP/SFTP push source has files on a box."""
    return source.transport_type == "push" and _config(source).get("push_method") == "ftp"


def parse_choice(raw: object, options: tuple[int, ...], what: str) -> int:
    try:
        value = int(str(raw))
    except ValueError:
        raise RetentionError("Choose how long to keep %s." % what) from None
    if value not in options:
        raise RetentionError("Choose one of the offered periods for %s." % what)
    return value


def set_retention(session: Session, source: ProviderSource, *, loader: int | None = None, box: int | None = None) -> None:
    config = _config(source)
    if loader is not None:
        config["retention_loader_days"] = parse_choice(loader, LOADER_OPTIONS, "files on the loader")
    if box is not None:
        config["retention_box_days"] = parse_choice(box, BOX_OPTIONS, "files on the FTP box")
    source.config = json.dumps(config)
    session.commit()


def carry_over(old_config: str | None, new_config: dict) -> dict:
    """An edit that rebuilds a source's config must not forget its retention."""
    try:
        old = json.loads(old_config or "{}")
    except ValueError:
        return new_config
    for key in KEYS:
        if isinstance(old, dict) and key in old and key not in new_config:
            new_config[key] = old[key]
    return new_config


# -- what is stored, and what would go -------------------------------------------------------------

def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def is_purged(file: HarvestedFile) -> bool:
    """A file whose contents were removed by retention: it had a size, and has no contents now."""
    return bool(file.size) and not file.content


def _candidates(source_id: str, cutoff: datetime.datetime):
    return (
        HarvestedFile.run_id.in_(select(HarvestRun.id).where(HarvestRun.source_id == source_id)),
        HarvestedFile.fetched_at < cutoff,
        HarvestedFile.size > 0,
        func.length(HarvestedFile.content) > 0,
    )


def preview(hsession: Session, source_id: str, days: int, *, now: datetime.datetime | None = None) -> tuple[int, int]:
    """(files, bytes) whose contents a ``days`` retention would remove right now."""
    if days <= 0:
        return 0, 0
    cutoff = (now or _now()) - datetime.timedelta(days=days)
    count, size = hsession.execute(
        select(func.count(), func.coalesce(func.sum(HarvestedFile.size), 0)).where(*_candidates(source_id, cutoff))
    ).one()
    return int(count), int(size)


def stored(hsession: Session, source_id: str) -> tuple[int, int, datetime.datetime | None]:
    """(files with contents, bytes, oldest) currently held for a source."""
    count, size, oldest = hsession.execute(
        select(func.count(), func.coalesce(func.sum(HarvestedFile.size), 0), func.min(HarvestedFile.fetched_at)).where(
            HarvestedFile.run_id.in_(select(HarvestRun.id).where(HarvestRun.source_id == source_id)),
            HarvestedFile.size > 0, func.length(HarvestedFile.content) > 0,
        )
    ).one()
    return int(count), int(size), oldest


def purge_source(hsession: Session, source_id: str, days: int, *, now: datetime.datetime | None = None) -> tuple[int, int]:
    """Empty the contents of this source's files older than ``days`` days. Returns (files, bytes)."""
    if days <= 0:
        return 0, 0
    cutoff = (now or _now()) - datetime.timedelta(days=days)
    files, size = preview(hsession, source_id, days, now=now)
    # A transform's output is kept as long as the file it came from: its contents go on the same day.
    hsession.execute(update(TransformedFile).where(
        TransformedFile.source_id == source_id, TransformedFile.created_at < cutoff,
        TransformedFile.size > 0, func.length(TransformedFile.content) > 0,
    ).values(content=b""))
    if not files:
        hsession.commit()
        return 0, 0
    hsession.execute(update(HarvestedFile).where(*_candidates(source_id, cutoff)).values(content=b""))
    hsession.commit()
    return files, size


def delete_source_data(hsession: Session, source_id: str) -> dict[str, int]:
    """Everything stored for a source that is being deleted: its transformed outputs, its stored files and records, and its runs. Nothing
    can reach them once the source is gone, so nothing is kept. Returns how many of each were removed."""
    runs = select(HarvestRun.id).where(HarvestRun.source_id == source_id)
    outputs = hsession.execute(delete(TransformedFile).where(TransformedFile.source_id == source_id)).rowcount or 0
    outputs += hsession.execute(delete(TransformedFile).where(TransformedFile.run_id.in_(runs))).rowcount or 0
    files = hsession.execute(delete(HarvestedFile).where(HarvestedFile.run_id.in_(runs))).rowcount or 0
    hsession.execute(delete(HarvestedRecord).where(HarvestedRecord.run_id.in_(runs)))
    run_count = hsession.execute(delete(HarvestRun).where(HarvestRun.source_id == source_id)).rowcount or 0
    hsession.commit()
    return {"runs": int(run_count), "files": int(files), "outputs": int(outputs)}


def purge_all(config_session: Session, hsession: Session, *, now: datetime.datetime | None = None) -> list[tuple[str, int, int]]:
    """One pass over every source. Returns [(source name, files, bytes)] for those that purged something."""
    done = []
    for source in config_session.scalars(select(ProviderSource)).all():
        files, size = purge_source(hsession, source.id, loader_days(source), now=now)
        if files:
            logger.info("retention: removed the contents of %d file(s), %d bytes, older than %s from %s",
                        files, size, label(loader_days(source)), source.name)
            done.append((source.name, files, size))
    return done


def start_purger(config_session_factory, harvest_session_factory_provider: Callable[[], Callable],
                 interval_s: int = 3600, first_after_s: int = 120, stop_event: threading.Event | None = None) -> threading.Thread | None:
    """A daemon thread that runs ``purge_all`` hourly. Off with ``LOADER_RETENTION=0``. A pass that
    fails (no harvest database configured, say) is logged and tried again next hour."""
    if os.environ.get("LOADER_RETENTION", "1") == "0":
        logger.info("retention: disabled via LOADER_RETENTION=0")
        return None
    stop_event = stop_event or threading.Event()

    def loop() -> None:
        stop_event.wait(first_after_s)
        while not stop_event.is_set():
            try:
                with config_session_factory() as config_session, harvest_session_factory_provider()() as hsession:
                    purge_all(config_session, hsession)
            except Exception:  # noqa: BLE001 -- a bad pass must never end the loop
                logger.exception("retention: purge pass failed")
            stop_event.wait(interval_s)

    thread = threading.Thread(target=loop, name="retention-purger", daemon=True)
    thread.stop_event = stop_event
    thread.start()
    return thread
