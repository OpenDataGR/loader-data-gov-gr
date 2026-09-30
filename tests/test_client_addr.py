from starlette.requests import Request

from providerharvest_service.client_addr import sender_address


def _request(forwarded=None, peer=("10.1.2.3", 4000)):
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request({"type": "http", "headers": headers, "client": peer})


def test_the_gateways_view_of_the_client_is_used_and_its_port_dropped():
    # App Gateway puts "<client>:<port>", then nginx appends the gateway's own address.
    assert sender_address(_request("203.0.113.9:51234, 10.0.0.4")) == "203.0.113.9"


def test_a_forged_leading_entry_is_ignored():
    # Whatever the client itself put in the header sits in front of the trusted part.
    assert sender_address(_request("9.9.9.9, 6.6.6.6, 203.0.113.9:51234, 10.0.0.4")) == "203.0.113.9"


def test_ipv6_with_and_without_brackets():
    assert sender_address(_request("[2001:db8::1]:443, 10.0.0.4")) == "2001:db8::1"
    assert sender_address(_request("2001:db8::1, 10.0.0.4")) == "2001:db8::1"


def test_a_single_entry_is_taken_as_is():
    assert sender_address(_request("203.0.113.9")) == "203.0.113.9"
    assert sender_address(_request("203.0.113.9:51234")) == "203.0.113.9"


def test_without_the_header_the_socket_peer_is_used():
    assert sender_address(_request(None, peer=("198.51.100.7", 1234))) == "198.51.100.7"


def test_junk_is_never_returned():
    assert sender_address(_request("not-an-ip, also-not")) is None
    assert sender_address(_request(None, peer=("testclient", 50000))) is None
    assert sender_address(_request("<script>alert(1)</script>, 10.0.0.4")) is None
    assert sender_address(Request({"type": "http", "headers": [], "client": None})) is None
