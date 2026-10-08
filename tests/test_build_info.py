import pytest
import subprocess

from providerharvest_service import build_info


def test_get_build_info_reads_real_git_metadata_in_this_repo():
    info = build_info.get_build_info()
    assert info["commit"] != "unknown"
    assert info["build_time"].endswith("UTC")
    assert info["commit_url"] == "%s/commit/%s" % (build_info.GITHUB_REPO_URL, info["commit"])

    # Cross-check against git directly, rather than just asserting shape.
    expected_commit = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=build_info._REPO_ROOT
    ).decode().strip()
    assert info["commit"] == expected_commit


def test_get_build_info_falls_back_cleanly_outside_a_git_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(build_info, "_REPO_ROOT", tmp_path)
    info = build_info.get_build_info()
    assert info == {"commit": "unknown", "build_time": "unknown", "commit_url": None}


def test_get_build_info_falls_back_cleanly_when_git_binary_is_missing(monkeypatch):
    def fake_check_output(*args, **kwargs):
        raise FileNotFoundError("no such file: git")

    monkeypatch.setattr(build_info.subprocess, "check_output", fake_check_output)
    info = build_info.get_build_info()
    assert info["commit"] == "unknown"


def test_get_changelog_reads_real_commit_history_in_this_repo():
    # Not "== 5" -- CI's own checkout is shallow (fetch-depth: 1), so
    # there may genuinely be only one commit available to read, same
    # as production's full clone would only ever have fewer than
    # `limit` right after this repo's very first commit. And that one
    # commit may be a throwaway-workflow commit, which the changelog
    # hides on purpose, so even an empty list can be right.
    entries = build_info.get_changelog(limit=5)
    assert len(entries) <= 5
    for entry in entries:
        assert entry["date"]
        assert entry["message"]

    # Cross-check against git directly, rather than just asserting shape.
    subjects = subprocess.check_output(
        ["git", "log", "-n", "15", "--format=%s"], cwd=build_info._REPO_ROOT
    ).decode().splitlines()
    shown = [s[2:] if s.startswith("@ ") else s for s in subjects]
    expected_first_message = next((s for s in shown if not build_info.is_scaffolding(s)), None)
    if expected_first_message is None:
        assert entries == []  # nothing but scaffolding in view: nothing to show
    else:
        assert entries[0]["message"] == expected_first_message


def test_get_changelog_falls_back_to_an_empty_list_outside_a_git_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(build_info, "_REPO_ROOT", tmp_path)
    assert build_info.get_changelog() == []


def test_get_changelog_falls_back_to_an_empty_list_when_git_binary_is_missing(monkeypatch):
    def fake_check_output(*args, **kwargs):
        raise FileNotFoundError("no such file: git")

    monkeypatch.setattr(build_info.subprocess, "check_output", fake_check_output)
    assert build_info.get_changelog() == []


@pytest.mark.parametrize("message", [
    "One-off: live test of box resources and actions", "One-off proof: add a v4 deny case", "one-off: lower case",
    "Remove the one-off IPv6 limit proof", "Remove a stray scratch script", "  Remove the one-off box actions live test",
])
def test_throwaway_workflow_commits_are_recognised(message):
    assert build_info.is_scaffolding(message)


@pytest.mark.parametrize("message", [
    "Allowed senders accept IPv6", "Remove the old login page", "Add a one-off import option", "Fix one-off bug in sync",
])
def test_real_changes_are_never_hidden(message):
    assert not build_info.is_scaffolding(message)


def test_the_changelog_hides_scaffolding_strips_a_stray_at_and_still_fills_the_limit(monkeypatch):
    log = "\n".join([
        "2026-10-08\x1fOne-off: a live test", "2026-10-08\x1fRemove the one-off live test",
        "2026-10-08\x1f@ Data boxes: the HTTP box", "2026-10-07\x1fReal change A", "2026-10-07\x1fReal change B",
    ])
    monkeypatch.setattr(build_info.subprocess, "check_output", lambda *a, **k: log.encode())
    entries = build_info.get_changelog(limit=2)
    assert [e["message"] for e in entries] == ["Data boxes: the HTTP box", "Real change A"]
