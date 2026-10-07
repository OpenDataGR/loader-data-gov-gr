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
