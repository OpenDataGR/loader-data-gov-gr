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

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, LargeBinary, String
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
    files: Mapped[list["HarvestedFile"]] = relationship(
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


class TransformedFile(HarvestBase):
    """The output of a source's transform for one run (see transform_runner.py): the transformed bytes
    plus how it went. One per run (a re-run replaces it). ``status`` is "ok" or "error"; an error keeps
    the message and no content, and never touches the original ``HarvestedFile``. Its contents follow the
    same retention as the file it came from (emptied, row kept)."""

    __tablename__ = "transformed_file"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("harvest_run.id"), nullable=False, unique=True, index=True)
    source_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    transform_id: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False, default="")
    content_type: Mapped[str] = mapped_column(String, nullable=False, default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, default=b"")
    records_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stats: Mapped[str] = mapped_column(String, nullable=False, default="{}")  # JSON: lookup misses, dropped, conversion errors
    status: Mapped[str] = mapped_column(String, nullable=False, default="ok")
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class HarvestedFile(HarvestBase):
    """One fetched whole file from one run, for SFTP/SCP/FTP sources --
    HTTP sources use ``HarvestedRecord`` instead (per-record JSON, not
    whole files). Stored as a blob directly in this DB (bytea via
    ``LargeBinary``), not on a filesystem or in object storage: these
    sources are documented to feed several small files (each fetch well
    under 30s), so there's no real infra benefit yet to a separate
    storage layer -- same "own DB, prove the smallest slice" choice
    every other piece of this service's storage has made. Revisit if a
    source ever actually needs large files -- ``harvest_runner``'s own
    ``MAX_FILE_SIZE_BYTES`` cap exists specifically so an unexpectedly
    huge file errors the run instead of silently blowing up this
    table."""

    __tablename__ = "harvested_file"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("harvest_run.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False, default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    fetched_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    run: Mapped["HarvestRun"] = relationship(back_populates="files")
