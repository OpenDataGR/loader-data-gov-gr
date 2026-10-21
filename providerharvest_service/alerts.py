"""The alert monitor: every minute it looks for the problems a person would want to hear about at once -- and not only when they happen to open the right
page -- and keeps one ``Alert`` row per problem.

* **a source gone silent** -- a push source that normally sends every few minutes and has sent nothing for much longer than that;
* **a pull source that keeps failing** -- its last three runs all ended in an error;
* **a transform that keeps failing** -- the last three outputs of an enabled pipeline are all errors;
* **a CKAN output that failed** -- its last push was refused or could not be made;
* **an FTP box that does not report**, or reports a service down;
* **a source whose connection check says error.**

The monitor only *finds* problems; showing them is the job of the header bell, the toast on an open page and the Alerts page (``ui_alerts.py``). It is
cheap by design: a few indexed queries a minute, once for everybody, whoever has a page open. Several workers may run it at once -- every pass is
idempotent (a problem is one row, found again and again, and the unique key stops a second one).
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import os
import statistics
import threading
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from providerharvest_service import transform_runner
from providerharvest_service.harvest_models import HarvestRun, TransformedFile
from providerharvest_service.models import Alert, CkanTarget, FtpBoxStatus, ProviderSource, Transform

logger = logging.getLogger(__name__)

MIN_SILENCE = datetime.timedelta(minutes=15)  # a source that sends every minute is not "silent" after two
SILENCE_FACTOR = 4  # ... but after four times its usual gap it is
MIN_RUNS_TO_KNOW_THE_RHYTHM = 5
STOP_WATCHING_AFTER = datetime.timedelta(days=30)  # a source that has sent nothing for a month is not news every minute
FAILURES_IN_A_ROW = 3
BOX_SILENCE = datetime.timedelta(minutes=5)


@dataclasses.dataclass(frozen=True)
class Problem:
    key: str
    kind: str
    severity: str
    title: str
    detail: str = ""
    source_id: str | None = None
    link: str = ""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _naive(moment: datetime.datetime | None) -> datetime.datetime | None:
    if moment is None:
        return None
    return moment.astimezone(datetime.timezone.utc).replace(tzinfo=None) if moment.tzinfo is not None else moment


def _span(delta: datetime.timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 3600:
        return "%d min" % max(1, seconds // 60)
    if seconds < 86400:
        return "%d h %d min" % (seconds // 3600, seconds % 3600 // 60)
    return "%d days %d h" % (seconds // 86400, seconds % 86400 // 3600)


# -- finding the problems ------------------------------------------------------------------------------------------------------------------------------

def _silent_sources(session: Session, hsession: Session, now: datetime.datetime) -> list[Problem]:
    found = []
    for source in session.scalars(select(ProviderSource).where(ProviderSource.status == "active", ProviderSource.transport_type == "push")).all():
        times = sorted(_naive(t) for t in hsession.scalars(
            select(HarvestRun.started_at).where(HarvestRun.source_id == source.id).order_by(HarvestRun.started_at.desc()).limit(40)).all() if t is not None)
        if len(times) < MIN_RUNS_TO_KNOW_THE_RHYTHM:
            continue  # (no rhythm to judge by yet)
        gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:]) if b > a]
        usual = datetime.timedelta(seconds=statistics.median(gaps)) if gaps else datetime.timedelta(minutes=5)
        silence = now - times[-1]
        if silence > max(MIN_SILENCE, usual * SILENCE_FACTOR) and silence < STOP_WATCHING_AFTER:
            found.append(Problem("silent:%s" % source.id, "silent", "warning", "No file from %s for %s" % (source.title, _span(silence)),
                                 "It usually sends about every %s; the last file arrived %s UTC." % (_span(usual), times[-1].strftime("%Y-%m-%d %H:%M")),
                                 source.id, "/provider/sources/%s/runs" % source.id))
    return found


def _failing_pulls(session: Session, hsession: Session) -> list[Problem]:
    found = []
    for source in session.scalars(select(ProviderSource).where(ProviderSource.status == "active", ProviderSource.transport_type != "push")).all():
        runs = hsession.execute(select(HarvestRun.status, HarvestRun.error_message).where(HarvestRun.source_id == source.id)
                                .order_by(HarvestRun.started_at.desc()).limit(FAILURES_IN_A_ROW)).all()
        if len(runs) == FAILURES_IN_A_ROW and all(status == "error" for status, _ in runs):
            found.append(Problem("pull:%s" % source.id, "failing", "error", "%s: its last %d runs failed" % (source.title, FAILURES_IN_A_ROW), runs[0][1] or "",
                                 source.id, "/provider/sources/%s/runs" % source.id))
    return found


def _failing_transforms(session: Session, hsession: Session) -> list[Problem]:
    found = []
    for pipeline in session.scalars(select(Transform).where(Transform.enabled.is_(True))).all():
        outputs = hsession.execute(select(TransformedFile.status, TransformedFile.error_message).where(TransformedFile.transform_id == pipeline.id)
                                   .order_by(TransformedFile.created_at.desc()).limit(FAILURES_IN_A_ROW)).all()
        if len(outputs) == FAILURES_IN_A_ROW and all(status == "error" for status, _ in outputs):
            source = session.get(ProviderSource, pipeline.source_id)
            if source is None or source.status != "active":
                continue
            where = source.title if pipeline.name == transform_runner.DEFAULT_PIPELINE else "%s / %s" % (source.title, pipeline.name)
            found.append(Problem("transform:%s" % pipeline.id, "failing", "error", "The transform of %s failed on its last %d files" % (where, FAILURES_IN_A_ROW),
                                 outputs[0][1] or "", source.id, "/transform/sources/%s" % source.id))
    return found


def _ckan_failures(session: Session) -> list[Problem]:
    found = []
    for target in session.scalars(select(CkanTarget).where(CkanTarget.last_status == "error")).all():
        source = session.get(ProviderSource, target.source_id)
        found.append(Problem("ckan:%s" % target.id, "ckan", "error", "CKAN: %s could not be updated" % (target.dataset_title or target.dataset_name or target.resource_name or "an output"),
                             target.last_error or "", target.source_id, "/load" if source is None else "/load/sources/%s/ckan?load=%s" % (source.id, target.id)))
    return found


def _boxes(session: Session, now: datetime.datetime) -> list[Problem]:
    found = []
    for box in session.scalars(select(FtpBoxStatus)).all():
        reported = _naive(box.reported_at)
        if reported is not None and now - reported > BOX_SILENCE:
            found.append(Problem("box:%s" % box.hostname, "box", "error", "The FTP box %s has not reported for %s" % (box.hostname, _span(now - reported)),
                                 "Its last report was at %s UTC. Files cannot be received while it is down." % reported.strftime("%Y-%m-%d %H:%M"), None, "/admin/ftp"))
            continue
        down = [name for name, ok in (("ProFTPD", box.proftpd_active), ("the file relay", box.relay_service_active), ("the FTP port", box.ftp_port_listening),
                                      ("the SFTP port", box.sftp_port_listening)) if not ok]
        if down:
            found.append(Problem("box:%s" % box.hostname, "box", "error", "The FTP box %s reports that %s is down" % (box.hostname, " and ".join(down)), "", None, "/admin/ftp"))
    return found


def _health(session: Session) -> list[Problem]:
    return [Problem("health:%s" % s.id, "health", "error", "%s: the connection check says error" % s.title, s.last_check_message or "", s.id, "/provider/sources/%s/runs" % s.id)
            for s in session.scalars(select(ProviderSource).where(ProviderSource.status == "active", ProviderSource.last_check_status == "error")).all()]


def find_problems(session: Session, hsession: Session, now: datetime.datetime | None = None) -> list[Problem]:
    now = now or _utcnow()
    problems: list[Problem] = []
    for finder in (lambda: _silent_sources(session, hsession, now), lambda: _failing_pulls(session, hsession), lambda: _failing_transforms(session, hsession),
                   lambda: _ckan_failures(session), lambda: _boxes(session, now), lambda: _health(session)):
        try:
            problems.extend(finder())
        except Exception:  # noqa: BLE001 -- one check failing must not blind the others
            logger.exception("alerts: a check failed")
    return problems


# -- keeping the rows up to date -----------------------------------------------------------------------------------------------------------------------

def sync(session: Session, problems: list[Problem], now: datetime.datetime | None = None) -> dict[str, int]:
    """Make the alert rows say what ``problems`` say: a new problem is opened, one that is still there is refreshed, one that came back is reopened and one
    that is gone is resolved. Returns how many were opened / resolved. The caller does not need to commit."""
    now = now or _utcnow()
    opened = resolved = 0
    existing = {a.key: a for a in session.scalars(select(Alert)).all()}
    current = {p.key for p in problems}
    for problem in problems:
        alert = existing.get(problem.key)
        if alert is None:
            session.add(Alert(key=problem.key, kind=problem.kind, severity=problem.severity, source_id=problem.source_id, title=problem.title, detail=problem.detail,
                              link=problem.link, first_seen=now, last_seen=now, changed_at=now))
            opened += 1
            continue
        reopened = alert.resolved_at is not None
        worse = alert.severity != problem.severity
        alert.last_seen = now
        alert.detail, alert.link, alert.kind, alert.source_id = problem.detail, problem.link, problem.kind, problem.source_id
        if reopened or worse:
            alert.severity, alert.title = problem.severity, problem.title
            alert.resolved_at, alert.acknowledged_by, alert.acknowledged_at, alert.changed_at = None, None, None, now
            opened += 1
        else:
            alert.title = problem.title  # (the length of a silence changes by the minute: that is not news)
    for key, alert in existing.items():
        if key not in current and alert.resolved_at is None:
            alert.resolved_at, alert.changed_at = now, now
            resolved += 1
    try:
        session.commit()
    except IntegrityError:  # another worker opened the same problem a moment ago: its row is as good as ours
        session.rollback()
    return {"opened": opened, "resolved": resolved}


def run_once(session: Session, hsession: Session, now: datetime.datetime | None = None) -> dict[str, int]:
    return sync(session, find_problems(session, hsession, now), now)


def start_monitor(config_session_factory, harvest_session_factory_provider: Callable[[], Callable], interval_s: int = 60, first_after_s: int = 45,
                  stop_event: threading.Event | None = None) -> threading.Thread | None:
    """A daemon thread that runs ``run_once`` every minute. Off with ``LOADER_ALERTS=0``. A pass that fails (no harvest database configured, say) is logged
    and tried again next minute."""
    if os.environ.get("LOADER_ALERTS", "1") == "0":
        logger.info("alerts: disabled via LOADER_ALERTS=0")
        return None
    stop_event = stop_event or threading.Event()

    def loop() -> None:
        stop_event.wait(first_after_s)
        while not stop_event.is_set():
            try:
                with config_session_factory() as session, harvest_session_factory_provider()() as hsession:
                    done = run_once(session, hsession)
                    if done["opened"] or done["resolved"]:
                        logger.info("alerts: %d opened, %d resolved", done["opened"], done["resolved"])
            except Exception:  # noqa: BLE001 -- a bad pass must never end the loop
                logger.exception("alerts: pass failed")
            stop_event.wait(interval_s)

    thread = threading.Thread(target=loop, name="alert-monitor", daemon=True)
    thread.stop_event = stop_event
    thread.start()
    return thread
