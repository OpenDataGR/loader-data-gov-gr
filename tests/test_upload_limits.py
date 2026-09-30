"""The app-level file-size cap (``harvest_runner.MAX_FILE_SIZE_BYTES``) is
only honest if nothing in front of the app rejects a file the app would
have accepted -- otherwise a provider is told "100 MB" and gets a bare
413/403 at 95. These pin the parts of that chain that live in this repo;
the Application Gateway's WAF file-upload limit is portal-only config and
has to be checked by hand (it was 100 MB when this was written).
"""

import pathlib
import re

from providerharvest_service import harvest_runner

NGINX_VHOST = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "nginx-loader.data.gov.gr"


def _nginx_body_limit_bytes() -> int:
    match = re.search(r"client_max_body_size\s+(\d+)([kmg]?)\s*;", NGINX_VHOST.read_text(encoding="utf-8"))
    assert match, "no client_max_body_size in the nginx vhost"
    units = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
    return int(match.group(1)) * units[match.group(2)]


def test_nginx_lets_through_at_least_what_the_app_would_accept():
    # The request body is the file plus multipart framing, so nginx needs
    # a little more than the cap itself.
    assert _nginx_body_limit_bytes() >= harvest_runner.MAX_FILE_SIZE_BYTES + 64 * 1024


def test_the_cap_is_not_absurdly_larger_than_nginx_needs():
    # Guards the other direction: raising the app cap without raising
    # nginx (or vice versa, leaving nginx far above) is drift worth noticing.
    assert _nginx_body_limit_bytes() <= 2 * harvest_runner.MAX_FILE_SIZE_BYTES
