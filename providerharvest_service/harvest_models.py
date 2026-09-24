"""ORM models for the harvested-data PostgreSQL store (see
harvest_db.py) -- a separate database from models.py's own SQLite
config (ProviderSource, FtpAccount, CkanUserToken), so a separate
``DeclarativeBase``/metadata too. ``HarvestRun.source_id`` references
``ProviderSource.id`` *logically*, not via a real foreign key -- the
two live in different database engines entirely, so the database
itself can't enforce that link. A run's ``source_name`` is
deliberately denormalized (copied at run time, not looked up live) so
a run's own history still reads sensibly even if its source is later
renamed or deleted.
"""

from __future__ import annotations

import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from providerharvest_service.models import _new_id, _utcnow


class HarvestBase(DeclarativeBase):
    pass


class HarvestRun(HarvestBase):
    """One execution attempt against a single provider source. ``status``:
    "running" (in progress -- set at creation, before anything that
    could fail), "success", or "error" (see ``error_message``).
    ``finished_at`` stays NULL while a run is still "running"."""

    __tablename__ = "harvest_run"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    source_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    source_name: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")
    records_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)

    records: Mapped[list["HarvestedRecord"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class HarvestedRecord(HarvestBase):
    """One fetched record from one run. ``ref`` is the transport
    Entry's own guid (a configured id field, or a content hash --
    see engine/transport/direct_https.py's ``_default_guid``).
    ``payload`` is the record's own parsed JSON body, stored as-is --
    no field-mapping/transformation applied yet (that's still on
    README's roadmap, same as the old blueprint's ``row_rules``)."""

    __tablename__ = "harvested_record"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("harvest_run.id"), nullable=False, index=True)
    ref: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    run: Mapped["HarvestRun"] = relationship(back_populates="records")
