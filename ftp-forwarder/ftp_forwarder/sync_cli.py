"""Entrypoint for the account-sync side: periodically pulls loader's
FTP-account list and mirrors it into this box's local ProFTPD SQLite
database. Its own process/systemd unit, separate from the file-relay
side (``cli.py``) -- account provisioning and file relaying are
different responsibilities that should fail independently, not share
a process where one crashing takes the other down with it."""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import time

import requests

from ftp_forwarder import access_rules, account_sync, archive_cleanup, box_commands, box_health, box_resources, ip_limits
from ftp_forwarder.attempts_log import AttemptsLogTail
from ftp_forwarder.auth_log import AuthLogTail
from ftp_forwarder.ssh_log import JournalSshTail


_NET_RATE = box_resources.NetRate()


def _resources(args) -> dict | None:
    """Load, memory, disk and sessions for loader's box page. Never raises: a monitoring
    figure must not be the reason the heartbeat does not go out."""
    try:
        return box_resources.collect(
            disks=("/", args.upload_root or ""), ftp_port=args.ftp_port, sftp_port=args.sftp_port, rate=_NET_RATE,
        )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("could not collect resource figures")
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Periodically syncs loader's FTP accounts into this box's local ProFTPD database"
    )
    parser.add_argument("--loader-base-url", required=True)
    parser.add_argument(
        "--sync-key-file", required=True,
        help="Path to a file holding the LOADER_FTP_SYNC_KEY value (never the value inline on the command line)",
    )
    parser.add_argument("--db-path", required=True, help="Path to the local SQLite DB ProFTPD's mod_sql_sqlite reads")
    parser.add_argument("--interval-s", type=int, default=60)
    parser.add_argument(
        "--cleanup-interval-s", type=int, default=6 * 3600,
        help="How often old forwarded files are removed from each account's archive, per loader's retention "
             "setting (see archive_cleanup.py); needs --upload-root",
    )
    parser.add_argument(
        "--upload-root",
        help="If set, create each synced account's home directory under this root (e.g. /srv/ftp) when it's missing",
    )
    parser.add_argument(
        "--upload-owner", default="virtualftp",
        help="User and group that own the created home directories (only used with --upload-root)",
    )
    parser.add_argument(
        "--auth-log", default="/var/log/proftpd/loader-auth.log",
        help="ProFTPD's login log (see auth_log.py); each provider's last login address is reported to loader",
    )
    parser.add_argument(
        "--attempts-log", default="/var/log/proftpd/loader-attempts.log",
        help="ProFTPD's log of every login attempt (see attempts_log.py); refused ones feed loader's failed-login report",
    )
    parser.add_argument(
        "--attempts-token-file", default="/etc/ftp-forwarder/secrets/attempts.token",
        help="The secret every genuine line of --attempts-log carries; lines without it are ignored as forgeries",
    )
    parser.add_argument(
        "--attempts-state", default="/var/lib/ftp-forwarder/attempts.state",
        help="Where the delivered-up-to position in --attempts-log is kept",
    )
    parser.add_argument(
        "--ssh-state", default="/var/lib/ftp-forwarder/ssh.state",
        help="Where the delivered-up-to position in the sshd journal is kept (see ssh_log.py)",
    )
    parser.add_argument("--ssh-unit", default="ssh", help="The systemd unit sshd runs as (journal source)")
    parser.add_argument(
        "--ip-limits-conf", default=ip_limits.DEFAULT_CONF_PATH,
        help="ProFTPD config file generated from each account's allowed addresses (see ip_limits.py); "
             "empty string turns the feature off",
    )
    parser.add_argument("--proftpd-unit", default="proftpd")
    parser.add_argument("--relay-unit", default="loader-ftp-forwarder")
    parser.add_argument("--fail2ban-unit", default="fail2ban")
    parser.add_argument(
        "--fail2ban-jail", action="append", dest="fail2ban_jails",
        help="A jail whose bans are reported and can be lifted; repeat for several "
             "(default: %s)" % ", ".join(box_health.DEFAULT_JAILS),
    )
    parser.add_argument("--ftp-port", type=int, default=21)
    parser.add_argument("--sftp-port", type=int, default=2222)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    jails = tuple(args.fail2ban_jails or box_health.DEFAULT_JAILS)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger(__name__)

    with open(args.sync_key_file, encoding="utf-8") as fh:
        sync_key = fh.read().strip()
    session = requests.Session()
    hostname = socket.gethostname()

    uid = gid = None
    if args.upload_root:
        # Linux-only lookups, and resolved once up front: a typo'd owner
        # should fail loudly at startup (systemd restarts it, the journal
        # says why), not silently create root-owned directories forever.
        import grp
        import pwd
        uid = pwd.getpwnam(args.upload_owner).pw_uid
        gid = grp.getgrnam(args.upload_owner).gr_gid

    auth_tail = AuthLogTail(args.auth_log)
    # Logins seen but not yet accepted by loader -- kept across cycles so a
    # failed report doesn't lose them (only the latest per user matters).
    pending_logins: dict = {}
    # The secret that marks a genuine line in the attempts log (see attempts_log.py:
    # the typed username is logged unescaped, so a stranger can forge lines). No
    # token readable means no way to tell real lines from forged ones, so that log
    # is not shipped at all -- the rest of the sync carries on.
    attempts_token = ""
    try:
        with open(args.attempts_token_file, encoding="utf-8") as fh:
            attempts_token = fh.read().strip()
    except OSError as exc:
        logger.error("cannot read %s (%s): failed FTP/SFTP logins will not be reported", args.attempts_token_file, exc)
    attempts_tail = AttemptsLogTail(args.attempts_log, args.attempts_state, attempts_token)
    ssh_tail = JournalSshTail(args.ssh_state, unit=args.ssh_unit)
    # Starts with nothing applied: the chain lives in memory, so after a restart (or a
    # reboot) loader's rules are put back at the first heartbeat.
    enforcer = access_rules.Enforcer(jails=jails)
    executor = box_commands.Executor(
        proftpd_unit=args.proftpd_unit, relay_unit=args.relay_unit, fail2ban_unit=args.fail2ban_unit,
        ssh_unit=args.ssh_unit,
    )
    # Results of actions run but not yet accepted by loader -- re-sent until they are, never re-run.
    pending_results: dict[str, dict] = {}
    # Actions already run (ids), so a repeated reply cannot run one twice. Loader sends each at most once,
    # this is the second lock.
    ran: set[str] = set()

    last_cleanup = float("-inf")  # the archive cleanup runs every few hours, and once at start
    while True:
        pending_logins.update(auth_tail.poll())
        sync_ok = True
        sync_message = "ok"
        try:
            account_sync.sync_once(
                session, args.loader_base_url, sync_key, args.db_path,
                upload_root=args.upload_root, uid=uid, gid=gid,
                ip_limits_conf=args.ip_limits_conf,
            )
        except (account_sync.SyncError, OSError) as exc:
            logger.error("sync failed: %s", exc)
            sync_ok = False
            sync_message = str(exc)

        if args.upload_root and time.monotonic() - last_cleanup > args.cleanup_interval_s:
            last_cleanup = time.monotonic()
            try:
                archive_cleanup.prune(args.db_path, args.upload_root)
            except Exception:  # noqa: BLE001 -- housekeeping must never stop the sync loop
                logger.exception("archive cleanup failed")
        health = box_health.collect_status(
            proftpd_unit=args.proftpd_unit,
            relay_unit=args.relay_unit,
            fail2ban_unit=args.fail2ban_unit,
            ftp_port=args.ftp_port,
            sftp_port=args.sftp_port,
        )
        report = account_sync.report_status(
            session, args.loader_base_url, sync_key,
            hostname=hostname,
            health=health,
            account_count=account_sync.local_account_count(args.db_path),
            sync_ok=sync_ok,
            sync_message=sync_message,
            clients=[{"username": login.username, "ip": login.ip} for login in pending_logins.values()],
            banned_ips=box_health.read_banned_in(jails),
            access_version=enforcer.applied_version or "",
            resources=_resources(args),
            command_results=list(pending_results.values()),
        )
        if report.accepted:
            pending_logins.clear()
            pending_results.clear()
            # Actions an admin asked for: run the ones on this box's own list, once each; the
            # result goes back with the next heartbeat.
            for command in report.commands:
                command_id = command.get("id") if isinstance(command, dict) else None
                if command_id in ran:
                    continue
                result = executor.execute(command)
                if result is not None:
                    ran.add(result.id)
                    pending_results[result.id] = result.as_payload()
                    logger.info("ran %r at an admin's request: %s", command.get("action"), "ok" if result.ok else "failed")
            # Loader's whitelist and blacklist. Any problem leaves what is in force alone and
            # shows as "waiting" on loader's side, until the next heartbeat tries again.
            try:
                access = access_rules.parse(report.access)
                if access is not None and enforcer.apply(access):
                    logger.info("access rules %s applied", access.version)
            except access_rules.AccessError as exc:
                logger.error("could not apply loader's access rules: %s", exc)
            # Addresses an admin asked loader to let back in; the next report
            # then shows them gone from the ban list, which closes the request.
            for address in report.unban:
                if box_health.unban_everywhere(address, jails):
                    logger.info("lifted the ban on %s at an admin's request", address)
                else:
                    logger.error("could not lift the ban on %r", address)
        # Refused logins for the report: the read position only moves on once
        # loader has accepted the batch. A busy scanner day is drained a batch
        # per cycle without sleeping in between.
        # Two sources, same rule: ProFTPD's attempts log (FTP, SFTP/SCP) and the
        # sshd journal (SSH on 22 and 7002).
        for name, tail in (("attempts log", attempts_tail), ("sshd journal", ssh_tail)):
            while True:
                try:
                    attempts, position = tail.poll()
                    if not attempts:
                        tail.commit(position)  # nothing to send, but skip past unrelated lines
                        break
                    if not account_sync.report_attempts(
                        session, args.loader_base_url, sync_key,
                        hostname=hostname, attempts=[a.as_payload() for a in attempts],
                    ):
                        break
                    tail.commit(position)
                except OSError as exc:
                    logger.error("could not process the %s: %s", name, exc)
                    break
        time.sleep(args.interval_s)


if __name__ == "__main__":
    sys.exit(main())
