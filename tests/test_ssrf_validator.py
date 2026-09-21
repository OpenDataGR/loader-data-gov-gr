import pytest

from providerharvest_service.engine.validators import (
    UnsafeNetworkTargetError,
    assert_safe_http_url,
    assert_safe_network_target,
)


def _resolver_returning(*ips):
    def resolver(hostname, port):
        return [(None, None, None, None, (ip, 0)) for ip in ips]
    return resolver


def test_rejects_loopback():
    with pytest.raises(UnsafeNetworkTargetError):
        assert_safe_http_url("http://example.com/api", resolver=_resolver_returning("127.0.0.1"))


def test_rejects_cloud_metadata_link_local():
    with pytest.raises(UnsafeNetworkTargetError):
        assert_safe_http_url(
            "http://example.com/api", resolver=_resolver_returning("169.254.169.254")
        )


def test_rejects_private_range_by_default():
    with pytest.raises(UnsafeNetworkTargetError):
        assert_safe_http_url("http://example.com/api", resolver=_resolver_returning("10.0.0.5"))


def test_allows_private_range_when_explicitly_enabled():
    ips = assert_safe_http_url(
        "http://example.com/api",
        allow_private_ranges=True,
        resolver=_resolver_returning("10.0.0.5"),
    )
    assert ips == ["10.0.0.5"]


def test_rejects_one_bad_ip_among_several():
    with pytest.raises(UnsafeNetworkTargetError):
        assert_safe_http_url(
            "http://example.com/api",
            resolver=_resolver_returning("8.8.8.8", "127.0.0.1"),
        )


def test_allows_public_address():
    ips = assert_safe_http_url("http://example.com/api", resolver=_resolver_returning("8.8.8.8"))
    assert ips == ["8.8.8.8"]


def test_rejects_non_http_scheme():
    with pytest.raises(UnsafeNetworkTargetError):
        assert_safe_http_url("ftp://example.com/api", resolver=_resolver_returning("8.8.8.8"))


def test_rejects_embedded_credentials():
    with pytest.raises(UnsafeNetworkTargetError):
        assert_safe_http_url(
            "http://user:pass@example.com/api", resolver=_resolver_returning("8.8.8.8")
        )


def test_rejects_dns_failure():
    def failing_resolver(hostname, port):
        import socket
        raise socket.gaierror("no such host")

    with pytest.raises(UnsafeNetworkTargetError):
        assert_safe_http_url("http://nope.invalid/api", resolver=failing_resolver)


def test_network_target_host_port_rejects_private():
    with pytest.raises(UnsafeNetworkTargetError):
        assert_safe_network_target(
            "internal.local", 22, resolver=_resolver_returning("192.168.1.5")
        )


def test_network_target_rejects_bad_port():
    with pytest.raises(UnsafeNetworkTargetError):
        assert_safe_network_target("example.com", 99999, resolver=_resolver_returning("8.8.8.8"))
