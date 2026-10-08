import subprocess

from ftp_forwarder import box_resources


def _done(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


SS = """\
0 0 10.0.0.5:21 203.0.113.9:50001
0 0 10.0.0.5:21 203.0.113.9:50002
0 0 [2001:db8::5]:2222 [2001:db8::9]:50003
0 0 10.0.0.5:22 203.0.113.7:50004
0 0 10.0.0.5:210 203.0.113.7:50005
"""


def _proc(tmp_path, **files):
    defaults = {
        "loadavg": "0.52 0.40 0.31 1/200 12345\n",
        "meminfo": "MemTotal: 4096000 kB\nMemAvailable: 2048000 kB\nSwapTotal: 1024000 kB\nSwapFree: 1000000 kB\n",
        "uptime": "86400.55 1000.0\n",
    }
    defaults.update(files)
    for name, text in defaults.items():
        (tmp_path / name).write_text(text)
    return str(tmp_path)


def test_everything_is_read_and_converted(tmp_path):
    got = box_resources.collect(disks=("/", str(tmp_path)), proc=_proc(tmp_path), run=lambda c: _done(SS))
    assert got["load"] == [0.52, 0.40, 0.31]
    assert (got["mem_total_mb"], got["mem_available_mb"]) == (4000, 2000)
    assert got["swap_used_mb"] == 24  # 1000 MB total, 976 MB free (each rounded down from kB)
    assert got["uptime_s"] == 86400
    assert (got["ftp_sessions"], got["sftp_sessions"]) == (2, 1)  # port 210 and 22 are not 21 / 2222
    assert {d["path"] for d in got["disks"]} <= {"/", str(tmp_path)}


def test_a_figure_that_cannot_be_read_is_none_and_the_rest_still_report(tmp_path):
    proc = _proc(tmp_path, loadavg="garbage", meminfo="")
    got = box_resources.collect(disks=("",), proc=proc, run=lambda c: _done(code=1))
    assert got["load"] is None and got["mem_total_mb"] is None and got["swap_used_mb"] is None
    assert got["ftp_sessions"] is None and got["sftp_sessions"] is None
    assert got["uptime_s"] == 86400 and got["disks"] == []


def test_a_missing_proc_file_and_a_missing_ss_are_survived(tmp_path):
    def run(cmd):
        raise FileNotFoundError("ss")

    got = box_resources.collect(disks=("/nonexistent-path",), proc=str(tmp_path / "nowhere"), run=run)
    assert got["load"] is None and got["uptime_s"] is None and got["disks"] == [] and got["ftp_sessions"] is None


def test_the_same_disk_is_not_listed_twice(tmp_path):
    got = box_resources.collect(disks=(str(tmp_path), str(tmp_path)), proc=_proc(tmp_path), run=lambda c: _done(SS))
    assert len(got["disks"]) == 1


NET_DEV = """\
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0
  eth0: 5000 10 0 0 0 0 0 0 7000 12 0 0 0 0 0 0
docker0: 1 1 0 0 0 0 0 0 1 1 0 0 0 0 0 0
"""
ROUTE = "Iface\tDestination\tGateway\tFlags\nlo\t0000007F\t00000000\t1\neth0\t00000000\t0100000A\t3\n"
IP_ADDR = """\
2: eth0    inet 83.212.98.210/24 brd 83.212.98.255 scope global eth0\       valid_lft forever
2: eth0    inet6 2001:648:2ffc:1225:a800:7ff:fe70:87b9/64 scope global mngtmpaddr noprefixroute
2: eth0    inet 83.212.98.210/24 scope global secondary eth0
"""


def test_the_default_interface_and_its_counters_are_read(tmp_path):
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "route").write_text(ROUTE)
    (tmp_path / "net" / "dev").write_text(NET_DEV)
    assert box_resources.default_interface(str(tmp_path)) == "eth0"
    assert box_resources.net_counters("eth0", str(tmp_path)) == (5000, 7000)  # not lo, not docker0
    assert box_resources.net_counters("nope", str(tmp_path)) is None


def test_no_default_route_means_no_traffic_figures(tmp_path):
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "route").write_text("Iface\tDestination\nlo\t0000007F\n")
    (tmp_path / "net" / "dev").write_text(NET_DEV)
    got = box_resources.collect(disks=(), proc=_proc(tmp_path), run=lambda c: _done(""))
    assert got["net"] is None


def test_own_addresses_are_listed_once_ipv4_and_ipv6(tmp_path):
    assert box_resources.addresses(run=lambda c: _done(IP_ADDR)) == ["83.212.98.210", "2001:648:2ffc:1225:a800:7ff:fe70:87b9"]
    assert box_resources.addresses(run=lambda c: _done("", code=1)) == []

    def boom(cmd):
        raise FileNotFoundError("ip")

    assert box_resources.addresses(run=boom) == []


def test_a_rate_needs_two_readings_and_a_counter_going_backwards_restarts_it():
    rate = box_resources.NetRate()
    assert rate.update(1000, 2000, now=100.0) is None  # nothing to compare with yet
    assert rate.update(1000 + 60000, 2000 + 30000, now=160.0) == (1000, 500)
    assert rate.update(10, 20, now=220.0) is None  # the counters were reset (a reboot)
    assert rate.update(10 + 600, 20 + 120, now=280.0) == (10, 2)


def test_collect_reports_traffic_totals_and_the_rate_when_given_a_tracker(tmp_path):
    proc = _proc(tmp_path)
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "route").write_text(ROUTE)
    (tmp_path / "net" / "dev").write_text(NET_DEV)
    rate = box_resources.NetRate()
    first = box_resources.collect(disks=(), proc=proc, run=lambda c: _done(IP_ADDR), rate=rate)
    assert first["net"] == {"iface": "eth0", "rx_bytes": 5000, "tx_bytes": 7000, "rx_bps": None, "tx_bps": None}
    assert first["addresses"][0] == "83.212.98.210"
