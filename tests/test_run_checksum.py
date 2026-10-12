"""A run's file shows its SHA-256, so a provider can compare it with their own."""

import hashlib

from tests.test_transform_ui import (  # noqa: F401  (fixtures and helpers shared with the Transform Box tests)
    _isolated_db, _isolated_harvest_db, _master_key, _upload_data, client, source,
)


def test_a_runs_file_shows_the_sha256_of_what_the_loader_holds(client, source):
    content = b"<a>hello</a>"
    run_id = _upload_data(client, source, content=content, name="x.xml")
    page = client.get("/provider/sources/%s/runs/%s" % (source["id"], run_id)).text
    assert hashlib.sha256(content).hexdigest() in page and "SHA-256" in page


def test_a_file_whose_contents_were_removed_has_no_checksum(client, source, _isolated_harvest_db):
    from providerharvest_service.harvest_models import HarvestedFile

    content = b"<a>hello</a>"
    run_id = _upload_data(client, source, content=content, name="x.xml")
    with _isolated_harvest_db() as h:
        for row in h.query(HarvestedFile).all():
            row.content = b""
        h.commit()
    page = client.get("/provider/sources/%s/runs/%s" % (source["id"], run_id)).text
    assert hashlib.sha256(content).hexdigest() not in page and "removed (retention)" in page
