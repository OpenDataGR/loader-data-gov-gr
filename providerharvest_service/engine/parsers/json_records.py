from __future__ import annotations

import json
from typing import BinaryIO, Iterator

from providerharvest_service.engine.parsers.base import RecordParser


class JSONRecordsParser(RecordParser):
    """Parses a single JSON object, or a JSON array of objects, into one
    or more record dicts. (JSON-lines can be added here later if a
    provider needs it -- not required for Phase 1.)
    """

    def parse(self, stream: BinaryIO) -> Iterator[dict]:
        data = json.load(stream)
        if isinstance(data, list):
            for record in data:
                yield record
        elif isinstance(data, dict):
            yield data
        else:
            raise ValueError(
                "Expected a JSON object or array of objects, got %s" % type(data).__name__
            )
