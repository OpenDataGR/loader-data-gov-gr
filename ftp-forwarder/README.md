# ftp-forwarder

Relays files pushed via FTP/SFTP into
[`loader-data-gov-gr`](../README.md)'s own HTTPS push-ingest endpoint
(`POST /push/{source_name}/upload`), and keeps a local ProFTPD
instance's account database in sync with loader's own admin panel.

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

- **File relay** (`ftp_forwarder.cli`) -- watches each configured
  account's directory for a completed upload (`IN_CLOSE_WRITE`, not
  `IN_CREATE` -- see `watcher.py`'s own docstring for why that
  distinction matters) and forwards it, with retry-with-backoff and
  local archiving on success (see `sender.py`). A periodic catch-up
  sweep (`sweep.py`) independently re-scans every watched directory,
  so nothing pushed while this process wasn't running -- or that
  failed every retry last time -- gets silently missed.
- **Account sync** (`ftp_forwarder.sync_cli`) -- periodically pulls
  loader's current FTP-account list (`GET /admin/ftp-accounts-sync`,
  gated by its own narrow `LOADER_FTP_SYNC_KEY`, never the full admin
  key) and mirrors it into this box's own local SQLite database --
  the one ProFTPD's `mod_sql_sqlite` actually authenticates against.
  Loader's admin panel stays the one place accounts get created,
  edited, or disabled.

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

`ftp_forwarder.cli` takes one YAML file:

```yaml
loader_base_url: https://loader.data.gov.gr
accounts:
  - name: acme-provider              # a label, used only in this file/logs
    watch_dir: /srv/ftp/acme-provider
    source_name: acme-provider-push  # the loader push-mode source name
    api_key_file: /etc/ftp-forwarder/secrets/acme-provider.key
  - name: other-provider
    watch_dir: /srv/ftp/other-provider
    source_name: other-provider-push
    api_key_file: /etc/ftp-forwarder/secrets/other-provider.key

# All optional -- shown here at their defaults.
archive_subdir: .forwarded
max_retries: 5
retry_backoff_s: 1.0
worker_count: 8
catch_up_interval_s: 60
```

Each account's API key lives in its own, separately
permission-restricted (`chmod 600`) file, referenced by path -- never
inline in this config, same "secrets live in their own restricted
file" rule loader's own `/etc/loader-data-gov-gr.env` already follows.
See `deploy/config.example.yml`.

`ftp_forwarder.sync_cli` takes its settings as CLI flags instead (see
`deploy/loader-ftp-forwarder-sync.service`) -- it has far fewer of
them and no per-account structure, so a YAML file would be overkill.

## Running the tests

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows; drop .exe/Scripts on Linux/macOS
./.venv/Scripts/python.exe -m pytest tests
```

No real inotify needed: `config.py`/`sender.py`/`pool.py`/`sweep.py`/
`account_sync.py` are all plain, cross-platform-testable Python.
`watcher.py` (the only genuinely Linux-specific module, needing the
`inotify_simple` package -- install the `linux` extra to get it) is a
thin wrapper around a well-tested OS primitive, verified for real on
Linux by this repo's own CI (`.github/workflows/ftp-forwarder-tests.yml`,
which runs on `ubuntu-latest`), not unit-tested directly here.
