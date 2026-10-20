"""FTP-account credential helpers shared by the admin API (admin_ftp.py)
and provider self-service registration (provider_sources.py) -- kept
framework-agnostic (no FastAPI) so provider_sources.py, which
deliberately has no web-framework imports, can use them too.
"""

from __future__ import annotations

import os
import posixpath
import re
import secrets

import bcrypt

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")


def ftp_upload_root() -> str:
    # posixpath, not pathlib/os.path: this validates a path meant for
    # the Linux deployment target, regardless of what OS runs the code
    # (including this test suite, which may run on Windows).
    #
    # Default is the external FTP/SFTP-receiving box's own upload root
    # (see ftp-forwarder/README.md), not this VM's own -- that box is
    # the only one a real provider can actually reach (this VM has no
    # public IP, which is the entire reason that box exists). This VM's
    # own /srv/ftp-uploads was the original proof-of-concept location
    # (see deploy/README.md's FTP-push section); no real account should
    # be scoped there going forward.
    return posixpath.normpath(os.environ.get("LOADER_FTP_UPLOAD_ROOT", "/srv/ftp"))


def validate_home_directory(value: str) -> str:
    if not value.startswith("/"):
        raise ValueError("home_directory must be an absolute path")
    normalized = posixpath.normpath(value)
    root = ftp_upload_root()
    if normalized != root and not normalized.startswith(root + "/"):
        raise ValueError("home_directory must be under %s" % root)
    return normalized


def hash_password(password: str) -> str:
    # prefix=b"2a", not bcrypt's own $2b$ default: confirmed for real
    # against a live ProFTPD 1.3.8b + mod_sql_passwd instance that a
    # $2b$ hash is silently rejected (clean "530 Login incorrect", no
    # error logged) while the identical password re-hashed as $2a$
    # authenticates correctly. Not a guess -- the $2b$ hash was tested
    # first and failed; only $2a$ was confirmed working.
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(prefix=b"2a")
    ).decode("ascii")


def generate_password() -> str:
    return secrets.token_urlsafe(24)


def _minimum() -> int:
    """The shortest password somebody may choose for a push account. TEMPORARILY 6 (a provider's existing password has 7 characters and is to be changed
    in a few days); it was 12 and goes back to 12 -- set LOADER_MIN_PASSWORD_LENGTH on the VM, or change the default here. Never below 6."""
    try:
        return max(6, int(os.environ.get("LOADER_MIN_PASSWORD_LENGTH") or 6))
    except ValueError:
        return 6


MIN_CUSTOM_PASSWORD_LENGTH = _minimum()
# bcrypt only reads the first 72 bytes of a password: anything longer is cut off
# (or, in newer versions, refused), so a longer one would be a password that
# "works" with any ending. Refuse it up front instead.
MAX_PASSWORD_BYTES = 72


def password_problem(password: str) -> str | None:
    """Why a password somebody typed cannot be used, or None if it can."""
    if len(password) < MIN_CUSTOM_PASSWORD_LENGTH:
        return "at least %d characters" % MIN_CUSTOM_PASSWORD_LENGTH
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return "at most %d bytes (about %d characters) -- longer ones are cut short by the hashing" % (MAX_PASSWORD_BYTES, MAX_PASSWORD_BYTES)
    if any(not ch.isprintable() for ch in password):
        return "no control characters"
    return None
