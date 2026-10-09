"""A short history of each box's figures, drawn as small charts on the Data boxes page.

One sample per box every five minutes (``BUCKET_S``), kept seven days: enough to see whether load is
climbing, memory is leaking or a disk is filling, without becoming a monitoring system. A sample is
the handful of numbers that matter -- load, memory, disk, network in/out, open connections -- not
the whole report.

Where samples come from:

* an **FTP box**: whenever its heartbeat arrives (``admin_ftp``), at most one per five minutes;
* the **HTTP box** (loader itself): a small daemon thread in each worker, started by the app's
  lifespan like the harvest scheduler. All workers try; the unique index on (box, five-minute bucket)
  means exactly one wins.

A box that was silent leaves a hole, and the charts draw it as a hole (a gap in the line), not as a
straight line over data that does not exist.
"""

from __future__ import annotations

import datetime
import logging
import os
import threading
import time
from typing import Callable

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from providerharvest_service.models import BoxSample

logger = logging.getLogger(__name__)

BUCKET_S = 300
KEEP_FOR = datetime.timedelta(days=7)
GAP_S = 3 * BUCKET_S  # more than this between two samples: the box was not reporting
WINDOWS = {"6h": 6, "24h": 24, "7d": 168}
DEFAULT_WINDOW = "24h"
_last_prune = float("-inf")  # (not 0: the monotonic clock of a freshly started machine can be under an hour)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _bucket(at: datetime.datetime) -> int:
    return int(at.replace(tzinfo=datetime.timezone.utc).timestamp() // BUCKET_S)


def summarize(figures: dict) -> dict:
    """The few numbers worth keeping from a box's figures (see ``box_resources.collect``)."""
    load, cpus = figures.get("load"), figures.get("cpus") or 1
    total, avail = figures.get("mem_total_mb"), figures.get("mem_available_mb")
    disks = figures.get("disks") or []
    root = next((d for d in disks if d.get("path") == "/"), disks[0] if disks else None)
    net = figures.get("net") or {}
    sessions = [figures.get(k) for k in ("ftp_sessions", "sftp_sessions") if figures.get(k) is not None]
    if figures.get("http_connections") is not None:
        sessions = [figures["http_connections"]]
    return {
        "load_pct": round(100 * load[0] / cpus, 1) if isinstance(load, list) and load else None,
        "mem_pct": round(100 * (total - avail) / total, 1) if total and avail is not None else None,
        "disk_pct": round(100 * (root.get("used_mb") or 0) / root["total_mb"], 1) if root and root.get("total_mb") else None,
        "rx_bps": net.get("rx_bps"),
        "tx_bps": net.get("tx_bps"),
        "sessions": sum(sessions) if sessions else None,
    }


def has_sample(session: Session, hostname: str, at: datetime.datetime | None = None) -> bool:
    return session.scalar(select(BoxSample.id).where(
        BoxSample.hostname == hostname, BoxSample.bucket == _bucket(at or _now()),
    )) is not None


def record(session: Session, hostname: str, figures: dict | None, *, at: datetime.datetime | None = None) -> bool:
    """Store a sample for this five-minute bucket unless there is one already. Never raises into
    the caller (a heartbeat must not fail because of a chart)."""
    global _last_prune
    if not figures:
        return False
    at = at or _now()
    try:
        if has_sample(session, hostname, at):
            return False
        session.add(BoxSample(hostname=hostname, bucket=_bucket(at), at=at, **summarize(figures)))
        session.commit()
    except IntegrityError:  # another worker took this bucket a moment ago
        session.rollback()
        return False
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("could not record a figures sample for %s", hostname)
        return False
    if time.monotonic() - _last_prune > 3600:
        _last_prune = time.monotonic()
        try:
            session.execute(delete(BoxSample).where(BoxSample.at < at - KEEP_FOR))
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
    return True


def samples(session: Session, hostname: str, hours: int, *, now: datetime.datetime | None = None) -> list[BoxSample]:
    since = (now or _now()) - datetime.timedelta(hours=hours)
    return list(session.scalars(
        select(BoxSample).where(BoxSample.hostname == hostname, BoxSample.at >= since).order_by(BoxSample.at)
    ).all())


# -- charts ------------------------------------------------------------------------------------

W, H, PAD = 240, 56, 4  # a small multiple: wide enough to read a trend, small enough to sit four across


def _segments(rows, value: Callable[[BoxSample], float | None]):
    """Runs of consecutive samples that have a value and are no more than GAP_S apart."""
    runs: list[list[tuple[datetime.datetime, float]]] = []
    run: list[tuple[datetime.datetime, float]] = []
    previous = None
    for row in rows:
        v = value(row)
        if v is None or (previous is not None and (row.at - previous).total_seconds() > GAP_S):
            if run:
                runs.append(run)
            run = []
        if v is not None:
            run.append((row.at, float(v)))
            previous = row.at
        else:
            previous = None
    if run:
        runs.append(run)
    return runs


def chart(rows: list[BoxSample], *, title: str, unit: str, series: list[tuple[str, str, Callable[[BoxSample], float | None]]],
          start: datetime.datetime, end: datetime.datetime, fixed_max: float | None = None, fmt: Callable[[float], str] = lambda v: "%.0f" % v) -> dict:
    """One small chart as plain data + inline SVG (no script needed to see it). ``series`` is
    [(label, colour, getter)]: all on ONE axis -- never two scales in one chart."""
    runs = {label: _segments(rows, getter) for label, _, getter in series}
    values = [v for r in runs.values() for run in r for _, v in run]
    top = fixed_max if fixed_max is not None else max(values + [1.0])
    if fixed_max is not None and values and max(values) > top:
        top = max(values)  # a load above 100% still has to fit
    span = max((end - start).total_seconds(), 1.0)

    def x(at):
        return PAD + (W - 2 * PAD) * (at - start).total_seconds() / span

    def y(v):
        return H - PAD - (H - 2 * PAD) * (v / top if top else 0)

    paths, points = [], {}
    for label, colour, _ in series:
        d = []
        for run in runs[label]:
            if len(run) == 1:
                d.append("M%.1f %.1fh0.01" % (x(run[0][0]), y(run[0][1])))  # a lone sample: a dot
            else:
                d.append("M" + " L".join("%.1f %.1f" % (x(a), y(v)) for a, v in run))
        if d:
            paths.append('<path d="%s" fill="none" stroke="%s" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>' % (" ".join(d), colour))
        points[label] = [[a.strftime("%m-%d %H:%M"), round(v, 1)] for run in runs[label] for a, v in run]
    svg = ('<svg class="spark" viewBox="0 0 %d %d" preserveAspectRatio="none" role="img" aria-label="%s">'
           '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="var(--color-border)" stroke-width="1"/>%s'
           '<line class="spark-cursor" x1="0" y1="0" x2="0" y2="%d" stroke="var(--color-gray-700)" stroke-width="1" style="display:none"/></svg>'
           % (W, H, title, PAD, H - PAD, W - PAD, H - PAD, "".join(paths), H))
    last = {label: (runs[label][-1][-1][1] if runs[label] else None) for label, _, _ in series}
    peak = max(values) if values else None
    return {
        "title": title, "unit": unit, "svg": svg, "points": points, "empty": not values,
        "legend": [(label, colour) for label, colour, _ in series],
        "now": {label: (fmt(v) if v is not None else "--") for label, v in last.items()},
        "peak": fmt(peak) if peak is not None else "--",
        "scale": fmt(top),
        "start": start.strftime("%m-%d %H:%M"), "end": end.strftime("%m-%d %H:%M"),
    }


def _rate(v: float) -> str:
    for unit, size in (("MB/s", 1e6), ("kB/s", 1e3)):
        if v >= size:
            return "%.1f %s" % (v / size, unit)
    return "%d B/s" % v


BLUE, ORANGE = "#2a78d6", "#eb6834"  # slots 1 and 2 of the validated categorical palette


def charts(rows: list[BoxSample], hours: int, *, now: datetime.datetime | None = None) -> list[dict]:
    end = now or _now()
    start = end - datetime.timedelta(hours=hours)
    pct = lambda v: "%.0f%%" % v  # noqa: E731
    return [
        chart(rows, title="Load (% of CPUs busy)", unit="%", series=[("load", BLUE, lambda r: r.load_pct)], start=start, end=end, fixed_max=100, fmt=pct),
        chart(rows, title="Memory in use", unit="%", series=[("memory", BLUE, lambda r: r.mem_pct)], start=start, end=end, fixed_max=100, fmt=pct),
        chart(rows, title="Disk / used", unit="%", series=[("disk", BLUE, lambda r: r.disk_pct)], start=start, end=end, fixed_max=100, fmt=pct),
        chart(rows, title="Network", unit="B/s", series=[("in", BLUE, lambda r: r.rx_bps), ("out", ORANGE, lambda r: r.tx_bps)], start=start, end=end, fmt=_rate),
        chart(rows, title="Open connections", unit="", series=[("connections", BLUE, lambda r: r.sessions)], start=start, end=end),
    ]


def table(rows: list[BoxSample], limit: int = 12) -> list[dict]:
    """The latest readings as text, newest first -- the same numbers the charts draw, for anyone
    who would rather read them (and for screen readers)."""
    def show(v, f):
        return "--" if v is None else f(v)

    return [{
        "at": r.at.strftime("%m-%d %H:%M"), "load": show(r.load_pct, lambda v: "%.0f%%" % v), "mem": show(r.mem_pct, lambda v: "%.0f%%" % v),
        "disk": show(r.disk_pct, lambda v: "%.0f%%" % v), "rx": show(r.rx_bps, _rate), "tx": show(r.tx_bps, _rate),
        "sessions": show(r.sessions, str),
    } for r in reversed(rows[-limit:])]


# -- the HTTP box's own sampler -----------------------------------------------------------------

def start_sampler(session_factory, interval_s: int = 60, stop_event: threading.Event | None = None) -> threading.Thread | None:
    """A daemon thread that samples loader itself. Off with ``LOADER_BOX_HISTORY=0``."""
    if os.environ.get("LOADER_BOX_HISTORY", "1") == "0":
        return None
    from providerharvest_service import http_box

    stop_event = stop_event or threading.Event()

    def loop() -> None:
        while not stop_event.is_set():
            try:
                with session_factory() as session:
                    if not has_sample(session, http_box.NAME):  # cheap check first: collecting is not free
                        record(session, http_box.NAME, http_box.collect_resources(disks=("/",)))
            except Exception:  # noqa: BLE001 -- a bad tick must never end the loop
                logger.exception("box history: sampling failed")
            stop_event.wait(interval_s)

    thread = threading.Thread(target=loop, name="box-history", daemon=True)
    thread.stop_event = stop_event
    thread.start()
    return thread
