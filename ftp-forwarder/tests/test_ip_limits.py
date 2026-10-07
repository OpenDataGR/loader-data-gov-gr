import subprocess

import pytest

from ftp_forwarder import ip_limits
from ftp_forwarder.ip_limits import IpLimitError, apply, render


def _done(returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)


def test_no_limits_means_no_rules_at_all():
    text = render([{"username": "acme", "allowed_ips": []}, {"username": "other"}])
    assert "<IfUser" not in text and "<Global>" not in text
    assert text.startswith("#")  # still a valid (comment-only) file


def test_a_limited_account_gets_an_allow_list_and_denyall():
    text = render([{"username": "acme-provider", "allowed_ips": ["203.0.113.0/24", "198.51.100.7/32"]}])
    assert text.count("<IfUser acme-provider>") == 1
    assert "      Allow 198.51.100.7 203.0.113.0/24\n      DenyAll\n" in text  # host without /32, sorted
    assert text.count("<Global>") == 1 and text.rstrip().endswith("</Global>")


def test_only_limited_accounts_are_named_and_the_output_is_stable():
    accounts = [
        {"username": "zeta", "allowed_ips": ["10.0.0.1"]},
        {"username": "open", "allowed_ips": []},
        {"username": "alpha", "allowed_ips": ["10.0.0.2", "10.0.0.2/32", "10.0.0.1"]},
    ]
    text = render(accounts)
    assert "open" not in text
    assert text.index("alpha") < text.index("zeta")
    assert "Allow 10.0.0.1 10.0.0.2\n" in text  # duplicates folded
    assert render(list(reversed(accounts))) == text  # same file whatever the order


@pytest.mark.parametrize("bad", ["::ffff:1.2.3.4", "not-an-ip", "", "1.2.3.4; reboot", "300.1.1.1", "any", "1.2.3.4\nDenyAll"])
def test_a_value_that_is_not_an_address_or_range_fails_the_whole_render(bad):
    with pytest.raises(IpLimitError):
        render([{"username": "acme", "allowed_ips": ["203.0.113.9", bad]}])


@pytest.mark.parametrize("bad", ["a b", "x", "acme\n</IfUser>", "acme>", "a" * 33, "ac;me", ""])
def test_a_username_that_could_alter_the_config_fails_the_render(bad):
    with pytest.raises(IpLimitError):
        render([{"username": bad, "allowed_ips": ["203.0.113.9"]}])


def test_first_apply_writes_the_file_checks_it_and_reloads(tmp_path):
    conf = str(tmp_path / "limits.conf")
    calls = []
    changed = apply([{"username": "acme", "allowed_ips": ["203.0.113.9"]}], conf, run=lambda c: calls.append(c) or _done())
    assert changed is True
    assert "<IfUser acme>" in open(conf).read()
    assert calls == [["proftpd", "-t"], ["systemctl", "reload", "proftpd"]]  # validate, then reload, in that order


def test_an_unchanged_list_touches_nothing_and_does_not_reload(tmp_path):
    conf = str(tmp_path / "limits.conf")
    accounts = [{"username": "acme", "allowed_ips": ["203.0.113.9"]}]
    apply(accounts, conf, run=lambda c: _done())
    calls = []
    assert apply(accounts, conf, run=lambda c: calls.append(c) or _done()) is False
    assert calls == []


def test_a_change_reloads_again_and_clearing_the_last_limit_empties_the_rules(tmp_path):
    conf = str(tmp_path / "limits.conf")
    apply([{"username": "acme", "allowed_ips": ["203.0.113.9"]}], conf, run=lambda c: _done())
    assert apply([{"username": "acme", "allowed_ips": ["203.0.113.10"]}], conf, run=lambda c: _done()) is True
    assert "203.0.113.10" in open(conf).read()
    assert apply([{"username": "acme", "allowed_ips": []}], conf, run=lambda c: _done()) is True
    assert "<IfUser" not in open(conf).read()


def test_a_config_proftpd_rejects_is_rolled_back_and_retried_next_time(tmp_path):
    conf = str(tmp_path / "limits.conf")
    apply([{"username": "acme", "allowed_ips": ["203.0.113.9"]}], conf, run=lambda c: _done())
    before = open(conf).read()

    with pytest.raises(IpLimitError, match="rejected"):
        apply([{"username": "acme", "allowed_ips": ["203.0.113.10"]}], conf,
              run=lambda c: _done(1, "bad directive") if c[:2] == ["proftpd", "-t"] else _done())
    assert open(conf).read() == before  # what is on disk is what ProFTPD is running

    calls = []
    assert apply([{"username": "acme", "allowed_ips": ["203.0.113.10"]}], conf, run=lambda c: calls.append(c) or _done()) is True
    assert ["systemctl", "reload", "proftpd"] in calls  # not forgotten


def test_a_failed_reload_is_rolled_back_too(tmp_path):
    conf = str(tmp_path / "limits.conf")
    with pytest.raises(IpLimitError, match="reload"):
        apply([{"username": "acme", "allowed_ips": ["203.0.113.9"]}], conf,
              run=lambda c: _done(1, "unit not found") if c[0] == "systemctl" else _done())
    assert not (tmp_path / "limits.conf").exists()  # there was no file before, so none now


def test_a_bad_account_list_touches_nothing(tmp_path):
    conf = str(tmp_path / "limits.conf")
    calls = []
    with pytest.raises(IpLimitError):
        apply([{"username": "acme", "allowed_ips": ["not-an-ip"]}], conf, run=lambda c: calls.append(c) or _done())
    assert calls == [] and not (tmp_path / "limits.conf").exists()


def test_the_default_path_sorts_before_the_sftp_config():
    # <Global> must be read before the SFTP <VirtualHost>, as in the proof on the box.
    assert ip_limits.DEFAULT_CONF_PATH.rsplit("/", 1)[1] < "loader-sftp.conf"


def test_ipv6_entries_are_written_as_allow_rules_beside_ipv4_ones_in_a_stable_order():
    text = render([{"username": "acme", "allowed_ips": ["2001:DB8:5::/48", "2001:db8::9/128", "203.0.113.9", "198.51.100.0/24"]}])
    assert "      Allow 198.51.100.0/24 203.0.113.9 2001:db8::9 2001:db8:5::/48\n" in text
