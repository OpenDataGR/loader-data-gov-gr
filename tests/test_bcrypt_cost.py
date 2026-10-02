"""The suite runs bcrypt at its minimum cost (see conftest.py) to stay fast. These
tests make sure that can never leak into, or hide a weakening of, production."""

import inspect
import pathlib
import re

import bcrypt

from providerharvest_service.ftp_credentials import hash_password
from tests.conftest import _REAL_GENSALT, TEST_BCRYPT_ROUNDS


def test_the_real_default_cost_is_the_strong_one():
    # What production gets: the library's own default, untouched by the test patch.
    assert inspect.signature(_REAL_GENSALT).parameters["rounds"].default >= 12


def test_production_code_never_sets_a_bcrypt_cost_of_its_own():
    # Every hash in the service must use the library default, so the cheap setting
    # in the tests is the only place a lower cost exists -- and it can't be
    # smuggled in as an explicit "rounds=" in the service itself.
    package = pathlib.Path(__file__).resolve().parent.parent / "providerharvest_service"
    offenders = [
        "%s:%d" % (path.name, number)
        for path in package.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"gensalt\([^)]*rounds", line) or re.search(r"\brounds\s*=", line)
    ]
    assert offenders == []


def test_hashes_made_in_tests_are_still_real_bcrypt_that_verifies():
    hashed = hash_password("a-long-enough-secret")
    assert hashed.startswith("$2a$%02d$" % TEST_BCRYPT_ROUNDS)  # the format ProFTPD needs, at the test cost
    assert bcrypt.checkpw(b"a-long-enough-secret", hashed.encode("ascii"))
    assert not bcrypt.checkpw(b"a-different-secret", hashed.encode("ascii"))


def test_a_hash_made_at_the_strong_cost_still_verifies_here():
    # Verification reads the cost out of the hash, so a production hash (cost 12) is
    # checked correctly by a test process even though new test hashes are cheap.
    strong = bcrypt.hashpw(b"s3cret", _REAL_GENSALT(rounds=12, prefix=b"2a"))
    assert strong.startswith(b"$2a$12$") and bcrypt.checkpw(b"s3cret", strong)
