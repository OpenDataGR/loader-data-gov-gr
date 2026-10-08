# ftp-forwarder

Relays files pushed via FTP/SFTP into
[`loader-data-gov-gr`](../README.md)'s own trusted-relay ingest
endpoint (`POST /admin/ftp-relay/{username}/upload` -- see
`providerharvest_service/ftp_relay_ingest.py` on loader's side), and
keeps a local ProFTPD instance's account database in sync with
loader's own admin panel.

## Why this exists

Real FTP/SFTP receiving needs a public network path into a machine
that loader's own app server doesn't have (see loader's own
`push_ingest.py` docstring and README for the full reasoning: no
public IP on that VM, and Application Gateway can't carry raw
FTP/SFTP regardless of configuration). This package runs on a
**separate, dedicated box** that does have one, and bridges the two:
providers push files there over real FTP/SFTP, and this software
relays each one into loader over HTTPS -- the same push-ingest
endpoint any HTTPS-capable provider could call directly.

## Two independent pieces, two independent processes

- **File relay** (`ftp_forwarder.cli`) -- watches each account's
  directory (discovered from this box's own local accounts database,
  see `accounts.py` -- not a hand-maintained list, that database is
  the exact same one account-sync below keeps current) for a completed
  upload (`IN_CLOSE_WRITE`, not `IN_CREATE` -- see `watcher.py`'s own
  docstring for why that distinction matters) and forwards it, with
  retry-with-backoff and local archiving on success (see `sender.py`).
  A periodic re-scan (`sweep.py`, same interval as the watch-set
  refresh) independently re-checks every watched directory, so nothing
  pushed while this process wasn't running -- or that failed every
  retry last time -- gets silently missed, and a newly admin-created
  account starts being watched (or a deleted one stops) within one
  cycle, no restart needed.
- **Account sync** (`ftp_forwarder.sync_cli`) -- periodically pulls
  loader's current FTP-account list (`GET /admin/ftp-accounts-sync`,
  gated by its own narrow `LOADER_FTP_SYNC_KEY`, never the full admin
  key) and mirrors it into this box's own local SQLite database --
  the one ProFTPD's `mod_sql_sqlite` actually authenticates against.
  Loader's admin panel stays the one place accounts get created,
  edited, or disabled -- a provider registering a push source via
  FTP/SFTP creates their own account there too (see loader's README).
  With `--upload-root` set it also creates each account's home directory
  when it's missing (owned by `--upload-owner`, group-writable + setgid
  so the relay can work inside it) -- ProFTPD can't log a virtual user
  in without one, and only ever under that root, since this runs as
  root and the list comes from loader. Every cycle, this same process also reports
  this box's own health (`box_health.py` -- is ProFTPD active, are
  ports 21/2222 actually listening, is the file-relay service active,
  did the account pull itself succeed) to loader via
  `PUT /admin/ftp-box-status`, same sync key -- together with the address
  each provider last logged in from, read from ProFTPD's login log
  (`auth_log.py`; ProFTPD's own logs record neither a successful login nor
  any SFTP transfer, so provisioning adds a small `ExtendedLog` for it). This is the *only* way
  loader ever learns this box is alive: since this box only ever calls
  loader (see "pull-based and outbound-only" below), loader can't ask
  -- it can only remember what it was last told, which is what its own
  admin panel's "FTP box" page shows.

Deliberately separate processes/systemd units: account provisioning
and file relaying are different responsibilities that should fail
independently, not share a process where one crashing takes the other
down with it.

Both are **pull-based and outbound-only** -- this box calls loader
over HTTPS; loader never needs to be able to reach this box at all.

## Why a real FTP daemon here, not just OpenSSH's own SFTP subsystem

Loader's own app VM already proves out this project's preferred, most
minimal pattern for something needing SFTP alone: plain OpenSSH's
built-in `sftp-server` subsystem plus chrooted `Match User` blocks, no
separate daemon. That works *only* for SFTP, though -- plain FTP is a
genuinely different wire protocol OpenSSH can't serve. Since this
box's providers need real plain FTP too, ProFTPD is the right call
here (the same proven, already-debugged setup loader's own
`deploy/README.md` documents for the app VM's own FTP-push feature --
`mod_sql`-backed virtual users, `mod_sftp` for SFTP, one account
system, both protocols).

## Protection against password guessing

The FTP (21) and SFTP (2222) ports are open to the internet with no
firewall in front (the okeanos network here is "unprotected mode"), and
they are scanned constantly: the first day alone saw ~1,450 refused logins
-- 88% from one address -- cycling through `root`, `admin`, `ubuntu`,
`postgres` and the rest of the usual dictionary. None of those are real
accounts and generated passwords are long and random, so nothing got in;
but there was nothing stopping it either.

`provision-ftp-box.yml` therefore installs **fail2ban** with a jail
(`loader-ftp`) on the login log ProFTPD writes for both servers
(`/var/log/proftpd/loader-auth.log`; a refused login is a `PASS 530`
line): five refusals within ten minutes ban that address from both ports
for an hour, doubling for each repeat offence up to a week. The workflow
proves the filter matches a refused login (and not a successful one) on a
made-up log before enabling it, and prints the jail's status each run.

A legitimate provider who mistypes a password too often is banned like
anyone else and waits out the hour -- or an admin can let them back in
right away, from loader's **Security box** page (`/admin/security/failed-logins`): it lists
the addresses currently banned on each box, with an Unban button. Loader
can't reach the box, so the click only records a request
(`FtpUnbanRequest`); the box collects it from the reply to its next
heartbeat (about a minute), lifts the ban itself
(`box_health.unban` -- it accepts nothing but an IP address, since it runs
as root), and the heartbeat after that shows the address gone, which closes
the request. A request the box never acts on expires after 15 minutes.
The same page shows whether fail2ban is running at all (a dead jail would
otherwise fail silently). Run `provision-ftp-box.yml` with the address in
its `unban_ip` input if the page itself is unavailable.

A second jail, `loader-ftp-slow`, catches scanners that stay under the first
one's radar by trying once every few minutes: 8 refused logins in 6 hours
bans for a day (doubling for repeats, up to a week). It reads the same log
through the same filter, and its bans appear in the same ban list and are
lifted by the same Unban button.

### Report of failed logins

The ban list says who is blocked *now*; the **Security box**
report (`/admin/security/failed-logins`, the same page as the ban list) says who
has been knocking and how often. ProFTPD writes a second log,
`/var/log/proftpd/loader-attempts.log` (`%{%s}t %a %p %m %s <token> %U`: time,
client address, server port -- 21 FTP / 2222 SFTP --, command, reply code, a
secret token, username as typed), kept apart from the fail2ban log so that
filter keeps its shape. **The token matters:** ProFTPD writes the typed username
into the log unescaped (confirmed on this box: an SFTP username containing a
newline split one line into two, the second entirely the sender's), so a
stranger could forge report rows -- any address, any username. Only lines
carrying the token (generated at provisioning into
`/etc/ftp-forwarder/secrets/attempts.token`, root-only, never leaving the box)
are believed; with no readable token nothing from this log is shipped. The
fail2ban log is unaffected, as it records the authenticated user, not the typed one. The sync service ships the `PASS 530` lines to loader in batches of up
to 2000 (`POST /admin/ftp-login-attempts`). Its read position is kept in
`/var/lib/ftp-forwarder/attempts.state` and moves forward only once loader
accepted the batch, so nothing is lost or repeated across a failure or restart.

Port 2222 is shown as **SFTP/SCP**: both are one SSH login there, and a
refusal happens before the client says which it wanted, so they cannot be
told apart.

Loader keeps them 90 days (`FtpLoginAttempt`) and shows a per-address
summary (number of attempts, first/last, FTP vs SFTP, distinct usernames,
banned now) above the attempt-by-attempt log, with filters and CSV
downloads. The username is whatever a stranger typed: control characters are
dropped and the length capped on intake, the page escapes it, and the CSV
defuses spreadsheet formulas. Attempts from before this log existed are not
in the report.

The same report also covers loader's own HTTP side, recorded by loader itself
(`failed_auth.py`, no box involved): a wrong password on the web login (with
the username typed -- never the password), and any `401` for an API/sync key
or bearer token, filed as *Admin API key*, *FTP box key*, *Push API key* or
*Bearer token* by the path it hit. Capped at 30 rows per address per minute
so a flood of bad requests cannot flood the table.

**SSH on the box (ports 22 and 7002)** is covered too, as method *SSH*.
The sync service reads sshd's journal (`ssh_log.py`; one row per `Failed
password` line -- the other lines a scanner leaves describe the same attempt)
and ships it like the ProFTPD log, with a journal cursor kept in
`/var/lib/ftp-forwarder/ssh.state` that only advances once loader accepted
the batch. sshd logs the *client's* port, not its own, so the report cannot
say 22 or 7002; it shows 22 for both. The distro's sshd jail only banned on
port 22 (confirmed in the live iptables rules) with 10-minute bans, so
`provision-ftp-box.yml` overrides it: both ports, 5 failures in 10 minutes
bans for an hour (doubling for repeats, up to a week), and fails the run if
the ban rule doesn't cover 7002. The sshd jail's bans appear in the Security
box ban list and are lifted by the same Unban button. fail2ban is reloaded,
not restarted, on re-provisioning, because a restart flushes every current ban.

### Whitelist and blacklist from loader

Loader's Security box keeps a **whitelist** and a **blacklist** of IPv4 and
IPv6 addresses/ranges (see loader's README, "Address control"). The box learns them
from the reply to each heartbeat (`access`: `version`, `allow`, `block`) and
`access_rules.py` applies them, at most once per version and again after a
restart (the firewall chain lives in memory):

- **Blacklist**: dropped at the firewall, on every port (FTP, SFTP, SSH) -- a
  dedicated iptables chain `LOADER-ACCESS`, jumped to first in `INPUT`; IPv6
  entries go to the same-named chain in ip6tables (touched only when there are
  IPv6 entries, so a box without ip6tables is fine until one is added).
- **Whitelist**: written into fail2ban's `ignoreip` for every jail
  (`/etc/fail2ban/jail.d/zz-loader-access.local`, read after the per-jail files
  so it overrides them), fail2ban reloaded when that changes, and any ban a
  trusted address currently has is lifted. Inside the chain a `RETURN` rule for
  each trusted address comes *before* the drops, so one trusted address inside a
  blocked range is not dropped.

It is config for a root process built from data that crossed the network, so it
is re-validated here, independently of loader: nothing wider than a
/8 (IPv4) or /32 (IPv6), nothing overlapping loopback or IPv4-mapped IPv6, at most 500 entries per list; any bad entry
refuses the whole set and leaves what is in force alone. Commands are argument
lists, never a shell. The firewall part is applied first, so a fail2ban problem
cannot leave blocks unapplied; on any failure the version stays unapplied (loader
shows *waiting*) and the next heartbeat retries the lot. The heartbeat reports
the version applied (`access_version`; empty until the first apply).

### Per-account login limits (allowed addresses)

On loader's **FTP box** page each account has an *Allowed from* list. Empty
(the default) means the account works from anywhere. Once any IPv4 or IPv6 address or
range is listed, the account works **only** from those -- over FTP and SFTP/SCP
alike -- and is refused from everywhere else even with the right password. A
change takes effect within about a minute of the box's next sync.

Enforced by ProFTPD, not the firewall: `ip_limits.py` renders
`/etc/proftpd/conf.d/loader-ip-limits.conf` -- `<IfUser name>` +
`<Limit LOGIN>` with `Allow ...` / `DenyAll`, inside `<Global>` -- from the
account list the sync already fetches. This was proven on the box before being
built (a throwaway server on spare ports with three accounts: limited to the
connecting address, limited to another address, unlimited; each tried over FTP
and SFTP): the first and last got in, the second was refused with the right
password -- on both protocols. (`mod_wrap2_sql` is not an option; it doesn't
cover SFTP.) A refused login simply drops the connection rather than answering 530.

The file is config that a root process reads and its source crossed the
network, so it is built defensively: usernames and addresses are re-validated
on the box (loader validates too), any bad value fails the whole apply and
leaves the previous file in force, the new file is kept only if `proftpd -t`
accepts the whole configuration, and ProFTPD is reloaded only when the rules
actually changed. A failure shows as the box's *Sync: failing* with the reason.
The limits are applied *before* the account database is updated, so an account
is never loginable from anywhere for a moment ahead of its limit. IPv4 and IPv6 both:
ProFTPD (running with `UseIPv6 on`, as here) takes either in `Allow`, proven on this
box for FTP and SFTP over IPv6 and IPv4 (a dual-stack listener shows an IPv4 client as
an IPv4 address, so IPv4 entries keep working). An IPv4 address written as IPv6
(`::ffff:1.2.3.4`) is refused. Without `UseIPv6 on` an IPv6 entry is a fatal config
error, which `proftpd -t` catches before anything is reloaded.

For a provider's own account the list comes from the **allowed senders** of
its push source (set when the provider registers or edits it) and is shown
read-only on the FTP box page; only admin-made accounts are edited by hand
there.

Take care with a provider on a changing address: once listed addresses are
the only way in, they are locked out until it is updated here.

Plain FTP (port 21, unencrypted) stays open for now: a provider depends on it.

### Resources and actions from loader's FTP box page

Every heartbeat also carries the box's own figures (`box_resources.py`: load, memory,
swap, disk for `/` and the upload root, its own addresses, traffic on the default-route interface (totals
since boot, and bytes/s between two heartbeats), uptime, open FTP/SFTP sessions -- read from
`/proc` and `shutil.disk_usage`, only the session count asks `ss`; a figure that cannot
be read is left out, never a reason for the heartbeat to fail), shown as meters on loader's
FTP box page.

An admin can also ask the box to run an **action** from that page. Loader cannot connect
here, so the request waits on loader; the heartbeat reply hands it over (`commands`:
an id and an action *name*), `box_commands.py` runs it, and the answer goes back in the
next heartbeat (`command_results`). What may run is decided **on this box**, from a fixed
list -- restart ProFTPD, restart the file relay, reload fail2ban, and a read-only
diagnostics snapshot (uptime, memory, disk, unit states, recent log lines). Loader never
sends a command line, every command is an argument list (no shell), unknown names are
refused, and output is cut to 12000 characters. Each request is run at most once (a lost
reply does not repeat a restart); one nobody collected within 15 minutes expires. A **log
view** (`logs-<service>-<lines>`, service one of proftpd/sync/relay/fail2ban/sshd, lines one of
50/100/200/500, anything else refused) reads that unit's journal with `journalctl -u ... -n ...`, output
up to 40000 characters. There is
deliberately no shell or arbitrary command here: that stays on SSH.

## One-time setup on the box itself

`.github/workflows/provision-ftp-box.yml` (idempotent, safe to re-run)
handles everything *after* this -- ProFTPD install/config, the
account-sync service, generating SSH host keys -- but the very first
clone of this repo onto the box is a one-time, manually-authenticated
step the workflow deliberately doesn't try to automate (this repo is
private, so an unauthenticated `git clone` from the workflow itself
would fail -- same reason the main app VM's own `deploy.yml` only ever
does `git pull`, never an initial `clone`; see that repo's own
`deploy/README.md` for the identical pattern there):

```bash
sudo mkdir -p /opt/data-projects
sudo chown "$(whoami)":"$(whoami)" /opt/data-projects
git clone https://github.com/OpenDataGR/loader-data-gov-gr.git /opt/data-projects/loader-ftp-forwarder
```

(Use whichever git credential -- SSH deploy key or a PAT -- you'd
normally use for a private OpenDataGR clone; same as however the main
app VM's own persistent checkout was authenticated.)

Also register a self-hosted GitHub Actions runner on this box for this
same repo, with its own distinct name/label (`loader-ftp`, not the
main app VM's own `loader`) -- see this repo's own
`deploy/README.md` for the general runner-registration steps, and use
`--labels self-hosted,loader-ftp` here instead.

Once both of those are done, `workflow_dispatch` on
`provision-ftp-box.yml` (targeting the `loader-ftp`-labeled runner)
does the rest, and stays safe to re-run any time after.

## Config

No YAML file, no per-provider file to hand-edit on the box at all --
both entrypoints take their settings as CLI flags (see each one's own
systemd unit under `deploy/` for the real, deployed invocation):

`ftp_forwarder.cli` (the file relay):

```
--loader-base-url https://loader.data.gov.gr
--sync-key-file /etc/ftp-forwarder/secrets/ftp-sync.key
--accounts-db-path /etc/proftpd/ftp-accounts.db   # same DB account-sync keeps current
--archive-subdir .forwarded        # optional, shown at its default
--max-retries 5                    # optional
--retry-backoff-s 1.0               # optional
--worker-count 8                   # optional
--catch-up-interval-s 60           # optional -- also how often the account list itself is re-read
```

`ftp_forwarder.sync_cli` (account sync + box-health heartbeat):

```
--loader-base-url https://loader.data.gov.gr
--sync-key-file /etc/ftp-forwarder/secrets/ftp-sync.key
--db-path /etc/proftpd/ftp-accounts.db
--interval-s 60                    # optional
--upload-root /srv/ftp             # optional -- create missing account home directories under here
--upload-owner virtualftp          # optional -- owner of those directories
--auth-log /var/log/proftpd/loader-auth.log   # optional -- ProFTPD's login log, tailed for provider addresses
--attempts-log /var/log/proftpd/loader-attempts.log   # optional -- refused logins, shipped to loader's report
--attempts-state /var/lib/ftp-forwarder/attempts.state  # optional -- how far that log has been delivered
--ip-limits-conf /etc/proftpd/conf.d/loader-ip-limits.conf  # optional -- generated per-account login limits ("" = off)
--proftpd-unit proftpd             # optional
--relay-unit loader-ftp-forwarder  # optional
--fail2ban-unit fail2ban           # optional -- reported, so a dead jail shows on the admin page
--fail2ban-jail loader-ftp         # optional, repeatable (default: loader-ftp and loader-ftp-slow) -- whose bans are reported and lifted
--ftp-port 21                      # optional
--sftp-port 2222                   # optional
```

The one secret either process needs -- `LOADER_FTP_SYNC_KEY` -- lives
in its own restricted file (`chmod 640`, owned `root:virtualftp` so
both services can read it without either running as root; see
`provision-ftp-box.yml`), referenced by path, same "secrets live in
their own restricted file" rule loader's own
`/etc/loader-data-gov-gr.env` already follows. There is deliberately no
longer a *second*, per-provider secret on this box at all: which
push-mode source an account's uploads go to
(`FtpAccount.push_target_source_name`) is set once, in loader's own
admin panel (`/admin/ftp`), and looked up server-side by
`ftp_relay_ingest.py` when this box's shared sync key authenticates an
upload -- see that module's own docstring for the full reasoning.

## Running the tests

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows; drop .exe/Scripts on Linux/macOS
./.venv/Scripts/python.exe -m pytest tests
```

No real inotify needed: `config.py`/`accounts.py`/`sender.py`/`pool.py`/
`sweep.py`/`account_sync.py`/`box_health.py` are all plain,
cross-platform-testable Python. `watcher.py` (the only genuinely
Linux-specific module, needing the `inotify_simple` package -- install
the `linux` extra to get it) is a thin wrapper around a well-tested OS
primitive, verified for real on Linux by this repo's own CI
(`.github/workflows/ftp-forwarder-tests.yml`, which runs on
`ubuntu-latest`), not unit-tested directly here.
