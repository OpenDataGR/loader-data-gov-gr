import pytest

from providerharvest_service import sender_allowlist
from providerharvest_service.sender_allowlist import AllowlistError, parse, permits

DNS = {
    "sender.example.gr": ["203.0.113.9"],
    "two.example.gr": ["198.51.100.7", "198.51.100.8"],
    "big.example.gr": ["10.0.0.%d" % i for i in range(1, 30)],
    "empty.example.gr": [],
}


def fake_resolve(host):
    if host not in DNS:
        raise AllowlistError("could not look up %r: no such host" % host)
    return DNS[host]


def test_nothing_typed_means_no_restriction():
    for text in (None, "", "   \n ,; "):
        assert parse(text, resolve=fake_resolve) == []


def test_addresses_and_ranges_are_stored_in_canonical_form():
    assert parse("203.0.113.9", resolve=fake_resolve) == ["203.0.113.9/32"]
    assert parse("198.51.100.7/24", resolve=fake_resolve) == ["198.51.100.0/24"]  # a host with a prefix -> its network


def test_entries_may_be_separated_by_lines_commas_semicolons_or_spaces_and_are_deduplicated_and_ordered():
    got = parse("203.0.113.9, 10.0.0.0/8;203.0.113.9\n  198.51.100.0/24 10.0.0.0/8", resolve=fake_resolve)
    assert got == ["10.0.0.0/8", "198.51.100.0/24", "203.0.113.9/32"]


def test_a_hostname_is_looked_up_now_and_its_addresses_are_what_is_stored():
    assert parse("sender.example.gr", resolve=fake_resolve) == ["203.0.113.9/32"]
    assert parse("two.example.gr", resolve=fake_resolve) == ["198.51.100.7/32", "198.51.100.8/32"]


def test_a_url_or_host_with_a_port_or_path_is_reduced_to_its_host():
    for text in ("https://sender.example.gr/upload?x=1", "sender.example.gr:8443", "sender.example.gr/some/path",
                 "ftp://user@sender.example.gr:21/"):
        assert parse(text, resolve=fake_resolve) == ["203.0.113.9/32"], text
    assert parse("http://203.0.113.9:8080/x", resolve=fake_resolve) == ["203.0.113.9/32"]


@pytest.mark.parametrize("bad", [
    "not an address",  # becomes two tokens, "not" has no dot
    "any", "all", "localhost", "*", "0", "300.1.1.1", "203.0.113.0/99", "2001:db8::/129", "::ffff:203.0.113.9",
    "[::1]:21x", "-bad.example.gr", "bad_.example.gr", "a..b.gr", "1.2.3.4;reboot x",
])
def test_things_that_are_not_an_address_range_or_hostname_are_refused_with_a_message(bad):
    with pytest.raises(AllowlistError) as err:
        parse(bad, resolve=fake_resolve)
    assert str(err.value)


def test_a_hostname_that_does_not_resolve_or_has_no_address_is_refused():
    with pytest.raises(AllowlistError, match="look up"):
        parse("nowhere.example.gr", resolve=fake_resolve)
    with pytest.raises(AllowlistError, match="no IP address"):
        parse("empty.example.gr", resolve=fake_resolve)


def test_a_hostname_pointing_at_very_many_addresses_is_refused_not_truncated():
    with pytest.raises(AllowlistError, match="range"):
        parse("big.example.gr", resolve=fake_resolve)


def test_too_many_entries_are_refused():
    with pytest.raises(AllowlistError, match="at most"):
        parse(" ".join("10.0.%d.1" % i for i in range(sender_allowlist.MAX_ENTRIES + 1)), resolve=fake_resolve)


def test_the_real_resolver_failing_is_a_message_not_a_crash(monkeypatch):
    import socket

    def boom(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(sender_allowlist.socket, "getaddrinfo", boom)
    with pytest.raises(AllowlistError, match="look up"):
        parse("sender.example.gr")


def test_no_list_lets_everyone_in_including_an_unknown_address():
    assert permits([], "203.0.113.9") and permits(None, None) and permits([], "")


def test_a_list_lets_in_only_its_addresses():
    allowed = ["203.0.113.9/32", "198.51.100.0/24"]
    assert permits(allowed, "203.0.113.9")
    assert permits(allowed, "198.51.100.77")
    assert not permits(allowed, "203.0.113.10")
    assert not permits(allowed, "8.8.8.8")


def test_an_address_that_cannot_be_determined_is_refused_when_there_is_a_list():
    allowed = ["203.0.113.9/32"]
    for address in (None, "", "garbage", "203.0.113.9; drop"):
        assert not permits(allowed, address), address


def test_ipv6_addresses_ranges_and_mapped_senders():
    assert parse("2001:DB8::1, 2001:db8:5::/48 203.0.113.9") == ["203.0.113.9/32", "2001:db8::1/128", "2001:db8:5::/48"]
    allowed = ["203.0.113.9/32", "2001:db8:5::/48"]
    assert permits(allowed, "2001:db8:5::77") and not permits(allowed, "2001:db8:6::1")
    assert permits(allowed, "::ffff:203.0.113.9") and not permits(allowed, "::ffff:203.0.113.10")  # a dual-stack listener's view of IPv4
    assert not permits(["2001:db8:5::/48"], "203.0.113.9")


def test_a_hostname_gives_all_its_addresses_both_families():
    assert parse("sender.example.gr", resolve=lambda h: ["203.0.113.9", "2001:db8::9"]) == ["203.0.113.9/32", "2001:db8::9/128"]
