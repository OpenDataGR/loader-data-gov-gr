"""Build/version metadata for the UI footer -- lets an admin
immediately tell whether a given fix actually deployed, without
digging through CI logs.

Derived straight from git: deploy.yml's persistent clone on the VM
(/opt/data-projects/loader-data-gov-gr) is a real `git pull`-maintained
checkout, `.git` included -- so reading it directly works identically
in production and local dev, no extra deploy-time file-writing step
needed. "Build time" is the deployed commit's own commit timestamp,
not "when this process started" -- close enough in practice (deploy
runs automatically on every push to master) and avoids the extra
plumbing a true deploy-timestamp would need.
"""

from __future__ import annotations

import datetime
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
GITHUB_REPO_URL = "https://github.com/OpenDataGR/loader-data-gov-gr"

_PLACEHOLDER = {"commit": "unknown", "build_time": "unknown", "commit_url": None}


def _format_commit_time(iso_string: str) -> str:
    dt = datetime.datetime.fromisoformat(iso_string)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def get_build_info() -> dict:
    """Returns {"commit", "build_time", "commit_url"} -- a clearly-
    labeled "unknown" placeholder (never a crash) if this isn't running
    from a git checkout at all, e.g. a from-wheel install with no
    ``.git`` directory, or the ``git`` binary itself missing."""
    try:
        output = subprocess.check_output(
            ["git", "log", "-1", "--format=%h%n%cI"],
            cwd=_REPO_ROOT,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        commit, commit_iso = output.split("\n")
    except (OSError, subprocess.CalledProcessError, ValueError):
        return dict(_PLACEHOLDER)

    return {
        "commit": commit,
        "build_time": _format_commit_time(commit_iso),
        "commit_url": "%s/commit/%s" % (GITHUB_REPO_URL, commit),
    }


def get_changelog(limit: int = 50) -> list[dict]:
    """The last ``limit`` commits' own date + one-line message, for the
    footer's changelog modal -- shown in-app instead of sending an
    admin off to GitHub. Same "read straight from the deployed clone's
    own .git" approach as ``get_build_info``; an empty list (never a
    crash) if this isn't running from a git checkout at all."""
    try:
        output = subprocess.check_output(
            ["git", "log", "-n", str(limit), "--format=%ad\x1f%s", "--date=format:%Y-%m-%d"],
            cwd=_REPO_ROOT,
            stderr=subprocess.DEVNULL,
        ).decode()
    except (OSError, subprocess.CalledProcessError):
        return []

    entries = []
    for line in output.splitlines():
        if not line:
            continue
        date, _, message = line.partition("\x1f")
        entries.append({"date": date, "message": message})
    return entries
