"""Transport abstraction shared by every source of provider data.

The same shape covers small-JSON-record HTTP APIs and (in later phases)
whole-file SFTP/SCP/FTP sources, so ``GenericProviderHarvester`` never
branches on transport type -- it only calls ``list_entries``/``open_entry``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import BinaryIO, Optional


@dataclass
class Entry:
    """One unit of harvestable content.

    ``ref`` is a stable identifier used as the HarvestObject guid (for
    HTTP API records, typically a configured id field or a content hash;
    for files, typically the remote path).
    """

    ref: str
    metadata: dict = field(default_factory=dict)
    #: already-fetched content, when the listing call itself returned the
    #: full body (typical for small JSON API records). None means
    #: ``open_entry`` must still fetch it (typical for file-based sources).
    inline_data: Optional[bytes] = None


@dataclass
class Page:
    entries: list[Entry]
    next_cursor: Optional[str]


class Transport(abc.ABC):
    """One connection to a single provider source for the duration of a job."""

    @abc.abstractmethod
    def connect(self) -> None:
        """Establish the connection. Must re-validate the network target
        (see logic.validators) immediately before connecting -- DNS can
        rebind between registration time and this call."""

    @abc.abstractmethod
    def list_entries(self, cursor: Optional[str]) -> Page:
        """Return the next page of entries after ``cursor`` (None = first page)."""

    @abc.abstractmethod
    def open_entry(self, entry: Entry) -> BinaryIO:
        """Return a readable, streamable handle to this entry's raw bytes."""

    @abc.abstractmethod
    def close(self) -> None:
        ...

    def __enter__(self) -> "Transport":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
