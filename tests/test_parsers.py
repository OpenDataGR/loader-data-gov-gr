import io
import json

import pytest

from providerharvest_service.engine.parsers.json_records import JSONRecordsParser


def test_parses_single_object():
    stream = io.BytesIO(json.dumps({"a": 1}).encode())
    assert list(JSONRecordsParser().parse(stream)) == [{"a": 1}]


def test_parses_array_of_objects():
    stream = io.BytesIO(json.dumps([{"a": 1}, {"a": 2}]).encode())
    assert list(JSONRecordsParser().parse(stream)) == [{"a": 1}, {"a": 2}]


def test_rejects_scalar_json():
    stream = io.BytesIO(b"42")
    with pytest.raises(ValueError):
        list(JSONRecordsParser().parse(stream))
