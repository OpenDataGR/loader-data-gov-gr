"""Automatic recurring harvest scheduler: periodically runs
``harvest_runner.run_harvest`` on every active HTTP source whose own
``frequency`` interval (DAILY/WEEKLY/MONTHLY) has elapsed since its
last run -- no person needs to click "Harvest now" for a source that
just wants to be kept fresh on a schedule. ``MANUAL`` sources are never
picked up here; that's the entire point of that frequency value.

Deployed as ``gunicorn -w 4`` (see deploy/loader-data-gov-gr.service),
so every worker process runs its own copy of the background loop below
-- without coordination, all four would notice the same due source at
the same tick and harvest it four times. Coordinated with a Postgres
session-level advisory lock (``pg_try_advisory_lock``): only the
worker that wins it actually scans for due sources and starts runs
that tick; the other three see the lock held and skip harmlessly (the
next tick, ~``LOADER_SCHEDULER_INTERVAL_S`` later, any worker's still
eligible). A transaction-scoped lock (``pg_try_advisory_xact_lock``)
would have been simpler, but ``run_harvest`` commits the harvest
session partway through its own work (to persist the "running" row
before the fetch even starts) -- that would release an xact-scoped
lock after the very first due source, defeating the point. The
session-level lock is instead held for the whole tick and released
explicitly in a ``finally``, regardless of how many commits happen on
that session in between. In tests (a SQLite ``harvest_session`` stand-
in, single-process, no concurrency to guard against) the lock is
skipped entirely -- ``pg_try_advisory_lock`` doesn't exist there.
"""

from __future__ import annotations

import datetime
import logging
import os
import threading
from typing import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from providerharvest_service import harvest_runner, provider_sources
from providerharvest_service.harvest_models import HarvestRun
from providerharvest_service.models import ProviderSource, _utcnow

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

FREQUENCY_INTERVALS: dict[str, datetime.timedelta] = {
    "DAILY": datetime.timedelta(days=1),
    "WEEKLY": datetime.timedelta(days=7),
    "MONTHLY": datetime.timedelta(days=30),
}

# A run stuck in "running" longer than this (e.g. the worker that
# started it crashed mid-fetch) is treated as abandoned and picked up
# again, rather than blocking that source from ever being scheduled
# again.
STUCK_RUN_THRESHOLD = datetime.timedelta(hours=1)

_LOCK_KEY = 0x6C6F6164657234  # arbitrary, stable across the process ("loader4" as hex-ish bytes)


def _as_aware_utc(dt: datetime.datetime | None) -> datetime.datetime | None:
    """SQLite (used as the harvest DB stand-in in tests) doesn't
    preserve tzinfo on a ``DateTime(timezone=True)`` column -- it comes
    back naive on read even though it was stored tz-aware. Treat a
    naive value as UTC (the only thing ``_utcnow``/the model ever
    stores) rather than let a naive/aware subtraction raise."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=datetime.timezone.utc)


def due_sources(
    config_session: Session, harvest_session: Session, now: datetime.datetime | None = None
) -> list[ProviderSource]:
    """Every active source, of a transport type ``harvest_runner`` can
    actually run, whose own ``frequency`` interval has elapsed since its
    last run (or that has never run, or whose last run has been stuck
    "running" past ``STUCK_RUN_THRESHOLD``). A paused/pending/rejected
    source, an unsupported transport type, or a ``MANUAL`` one is never
    returned -- same restrictions ``harvest_runner.run_harvest`` itself
    enforces, checked here too so a tick doesn't even attempt a source
    it would just have to skip.
    """
    now = now or _utcnow()
    due = []
    for source in provider_sources.list_all(config_session):
        if source.status != "active" or source.transport_type not in harvest_runner.SUPPORTED_TRANSPORT_TYPES:
            continue
        interval = FREQUENCY_INTERVALS.get(source.frequency)
        if interval is None:  # MANUAL, or any future frequency this scheduler doesn't know yet
            continue
        runs = harvest_runner.list_runs(harvest_session, source.id, limit=1)
        last_run = runs[0] if runs else None
        if last_run is None:
            due.append(source)
            continue
        finished_at = _as_aware_utc(last_run.finished_at)
        if finished_at is None:
            if now - _as_aware_utc(last_run.started_at) >= STUCK_RUN_THRESHOLD:
                due.append(source)
            continue
        if now - finished_at >= interval:
            due.append(source)
    return due


def _try_acquire_lock(harvest_session: Session) -> bool:
    if harvest_session.get_bind().dialect.name != "postgresql":
        return True
    return bool(
        harvest_session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar()
    )


def _release_lock(harvest_session: Session) -> None:
    if harvest_session.get_bind().dialect.name != "postgresql":
        return
    harvest_session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
    harvest_session.commit()


def run_due_harvests(
    config_session_factory: SessionFactory,
    harvest_session_factory: SessionFactory,
    now: datetime.datetime | None = None,
) -> list[HarvestRun]:
    """One scheduling tick: acquire the cross-worker lock, find every
    due source, harvest each in turn, release the lock. Returns the
    runs actually started this tick (empty if another worker already
    holds the lock, or nothing is due). A single source raising past
    ``run_harvest``'s own defenses (``NotFound``/``InvalidTransition``/
    ``UnsupportedTransportType`` -- e.g. it was paused or edited to a
    non-HTTP transport in the moment between being listed as due and
    actually being harvested) is logged and skipped, not allowed to
    abort the rest of the tick.
    """
    harvest_session = harvest_session_factory()
    got_lock = False
    try:
        got_lock = _try_acquire_lock(harvest_session)
        if not got_lock:
            return []
        config_session = config_session_factory()
        try:
            now = now or _utcnow()
            results = []
            for source in due_sources(config_session, harvest_session, now=now):
                try:
                    run = harvest_runner.run_harvest(config_session, harvest_session, source.id)
                    # run_harvest's own commit(s) leave `run` expired
                    # (SQLAlchemy's default expire_on_commit) -- refresh
                    # it while the session is still open, then detach it,
                    # so the caller can still read its attributes after
                    # this function closes both sessions below.
                    harvest_session.refresh(run)
                    harvest_session.expunge(run)
                    results.append(run)
                except (
                    provider_sources.NotFound,
                    provider_sources.InvalidTransition,
                    harvest_runner.UnsupportedTransportType,
                ) as exc:
                    logger.warning("scheduler: skipping source %s this tick: %s", source.id, exc)
            return results
        finally:
            config_session.close()
    finally:
        if got_lock:
            _release_lock(harvest_session)
        harvest_session.close()


def start_background_thread(
    config_session_factory: SessionFactory,
    harvest_session_factory_provider: Callable[[], SessionFactory],
    interval_s: int | None = None,
    stop_event: threading.Event | None = None,
) -> threading.Thread | None:
    """Starts the scheduler's daemon loop, unless ``LOADER_SCHEDULER_
    ENABLED=0``. ``harvest_session_factory_provider`` is a zero-arg
    callable that itself returns a session factory (rather than the
    factory directly) so that, if ``LOADER_HARVEST_DB_URL`` isn't
    configured yet in this environment (e.g. local dev), that failure
    happens fresh on every tick -- caught and logged, not raised once
    at startup and never retried.

    The returned thread's own ``stop_event`` attribute (also settable
    via the ``stop_event`` param) lets a caller ask the loop to exit
    promptly -- used by the app's own shutdown ``lifespan`` for a clean
    stop instead of relying solely on ``daemon=True`` abruptly killing
    it at process exit, and by tests so a short-``interval_s`` thread
    doesn't keep spinning (with its monkeypatches already reverted)
    past the end of the test that started it.
    """
    if os.environ.get("LOADER_SCHEDULER_ENABLED", "1") == "0":
        logger.info("scheduler: disabled via LOADER_SCHEDULER_ENABLED=0")
        return None
    interval_s = interval_s or int(os.environ.get("LOADER_SCHEDULER_INTERVAL_S", "300"))
    stop_event = stop_event or threading.Event()

    def _loop() -> None:
        while not stop_event.is_set():
            try:
                harvest_session_factory = harvest_session_factory_provider()
                run_due_harvests(config_session_factory, harvest_session_factory)
            except Exception:  # noqa: BLE001 -- a bad tick must never kill the whole loop
                logger.exception("scheduler: tick failed")
            stop_event.wait(interval_s)  # unlike time.sleep, wakes up immediately once stop_event is set

    thread = threading.Thread(target=_loop, name="harvest-scheduler", daemon=True)
    thread.stop_event = stop_event
    thread.start()
    return thread
