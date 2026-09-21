from __future__ import annotations

import abc
from typing import BinaryIO, Iterator


class RecordParser(abc.ABC):
    """Bytes -> structured record dicts, decoupled from how those bytes
    were fetched (HTTP record body vs. a downloaded SFTP/FTP file)."""

    @abc.abstractmethod
    def parse(self, stream: BinaryIO) -> Iterator[dict]:
        ...
