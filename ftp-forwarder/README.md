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
  admin panel's "FTP subsystem" page shows.

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
right away: run `provision-ftp-box.yml` with the address in its
`unban_ip` input (it validates the value before use).

Not covered, deliberately left for later: enforcing each account's
per-account allowed-IP list on the box (stored in loader, not yet
applied), and whether plain FTP (unencrypted) should stay open at all.

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
--proftpd-unit proftpd             # optional
--relay-unit loader-ftp-forwarder  # optional
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
