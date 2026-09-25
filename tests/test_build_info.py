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
    entries = build_info.get_changelog(limit=5)
    assert len(entries) == 5
    for entry in entries:
        assert entry["date"]
        assert entry["message"]

    # Cross-check against git directly, rather than just asserting shape.
    expected_first_message = subprocess.check_output(
        ["git", "log", "-1", "--format=%s"], cwd=build_info._REPO_ROOT
    ).decode().strip()
    assert entries[0]["message"] == expected_first_message


def test_get_changelog_falls_back_to_an_empty_list_outside_a_git_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(build_info, "_REPO_ROOT", tmp_path)
    assert build_info.get_changelog() == []


def test_get_changelog_falls_back_to_an_empty_list_when_git_binary_is_missing(monkeypatch):
    def fake_check_output(*args, **kwargs):
        raise FileNotFoundError("no such file: git")

    monkeypatch.setattr(build_info.subprocess, "check_output", fake_check_output)
    assert build_info.get_changelog() == []
