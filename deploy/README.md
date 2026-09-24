# Deploying to MIN01RG96PVM02

Follows the same shared-VM pattern already used by `opencar-data-gov-gr`
on this VM (`/opt/data-projects/`, `localadmin`, one self-hosted GitHub
Actions runner per project, gunicorn+UvicornWorker under systemd, nginx
vhost keyed by `server_name`). Confirmed against the real, working
`opencar-data-gov-gr.service` and its nginx vhost before writing these.

- Port: **5002** (5001 is already `opencar-data-gov-gr`'s gunicorn --
  check `sudo ss -tlnp | grep gunicorn` before reusing either if a third
  project lands here later).
- Service account: **`localadmin`** (already exists, already owns
  `/opt/data-projects`).

## One-time setup, run on the VM itself

1. **Clone as `localadmin`**, so it owns the files:
   ```bash
   sudo -iu localadmin
   cd /opt/data-projects
   git clone https://github.com/OpenDataGR/loader-data-gov-gr.git
   cd loader-data-gov-gr
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
   If you (or anyone else) need to `git pull`/commit from this directory
   under your own account later, run once:
   ```bash
   git config --global --add safe.directory /opt/data-projects/loader-data-gov-gr
   ```

2. **Register the self-hosted runner.** On this repo:
   `Settings -> Actions -> Runners -> New self-hosted runner`, Linux/x64,
   use the download/config commands shown there (version-pinned per
   session -- don't reuse an old one). Label it `loader` (matches
   `runs-on: [self-hosted, loader]` in `deploy.yml`) and give it a unique
   name, e.g. `loader-runner-pvm02`:
   ```bash
   sudo -iu localadmin
   mkdir actions-runner-loader && cd actions-runner-loader
   # paste the download/extract commands GitHub gave you
   ./config.sh --url https://github.com/OpenDataGR/loader-data-gov-gr --token <TOKEN> \
     --labels self-hosted,loader --name loader-runner-pvm02
   ```
   Install as a persistent service (needs root):
   ```bash
   sudo -i
   cd /home/localadmin/actions-runner-loader
   ./svc.sh install localadmin
   ./svc.sh start
   ./svc.sh status
   ```

3. **Install the systemd unit**:
   ```bash
   sudo cp deploy/loader-data-gov-gr.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now loader-data-gov-gr
   sudo systemctl status loader-data-gov-gr
   curl -sf http://127.0.0.1:5002/health
   ```
   **The `--now` matters** -- `deploy.yml`'s reload step restarts the
   service with `pkill` + systemd's `Restart=always`, which only works
   on an *already-running* service. If this is `enable`d without
   `--now` (or `enable`/`start` are run separately and the start step
   is skipped), the first CI deploy's reload step fails with a
   completely silent `journalctl -- No entries --` -- no crash, no
   error, nothing to explain why, since the service was never started
   even once (hit for real during this project's own first deploy; see
   the `shared-vm-cicd-deploy` skill's Pitfalls section). Confirm
   `systemctl status` shows `active (running)` and the `curl` above
   returns `{"status":"ok"}` before triggering the first `deploy.yml`
   run.

4. **Install the nginx vhost**:
   ```bash
   sudo cp deploy/nginx-loader.data.gov.gr /etc/nginx/sites-available/loader
   sudo ln -s /etc/nginx/sites-available/loader /etc/nginx/sites-enabled/loader
   sudo nginx -t && sudo systemctl reload nginx
   curl -sf -H "Host: loader.data.gov.gr" http://127.0.0.1/health
   ```

No sudoers changes needed: `deploy.yml`'s reload step uses `pkill` +
systemd's `Restart=always` rather than `sudo systemctl restart`, since
this VM's sudoers requires a password for arbitrary sudo commands (a
plain `systemctl restart` in CI would just hang). `sudo systemctl
reset-failed` in that same step needs no password either -- confirm
that's still true here the same way it is for `opencar-data-gov-gr`
before relying on it.

5. **Admin API secret and the envelope-encryption master key.** The
   admin key gates `/admin/*` (503 otherwise); the master key is what
   `engine/secrets/envelope.py` uses to encrypt every provider
   credential and CKAN token at rest -- **without it, registering a
   provider source, running a connection check, or calling its proxy
   all 500 outright** (`MasterKeyNotConfigured`, raised from deep
   inside `provider_sources.register`/`connection_check`/`proxy.py`).
   Hit this for real in production once: the key had only ever been
   exported in a manually-started process's shell, not written here --
   invisible until that process was finally replaced by a real
   `systemctl restart`/`Restart=always` cycle, which doesn't inherit a
   human's old shell environment at all. Both belong in the same
   separate, permission-restricted file rather than inline in the
   (normally world-readable) unit file:
   ```bash
   sudo tee /etc/loader-data-gov-gr.env > /dev/null <<'EOF'
   LOADER_ADMIN_API_KEY=<generate with: python3 -c "import secrets; print(secrets.token_urlsafe(32))">
   LOADER_FTP_UPLOAD_ROOT=/srv/ftp-uploads
   CKAN_PROVIDERHARVEST_MASTER_KEY=<generate with: python3 -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())">
   EOF
   sudo chown localadmin:localadmin /etc/loader-data-gov-gr.env
   sudo chmod 600 /etc/loader-data-gov-gr.env
   sudo sed -i '/Environment=PYTHONUNBUFFERED=1/a EnvironmentFile=/etc/loader-data-gov-gr.env' \
     /etc/systemd/system/loader-data-gov-gr.service
   sudo cat /etc/systemd/system/loader-data-gov-gr.service   # verify the sed landed cleanly
   sudo systemctl daemon-reload
   sudo systemctl restart loader-data-gov-gr
   curl -s -H "X-Admin-Key: <the key above>" http://127.0.0.1:5002/admin/ftp-accounts   # expect []
   ```
   Verify the master key specifically, not just the admin key -- it's
   the one that silently keeps working right up until the next process
   restart if it's ever missing:
   ```bash
   curl -s -X POST http://127.0.0.1:5002/provider/sources/new \
     -d name=deploy-smoke-test -d title=t -d endpoint_url=https://example.com \
     -d transport_type=http -d auth_type=api_key -d api_key=x
   # expect a 303 redirect with flash=...registered..., NOT a 500
   ```
   The actual key currently in use lives wherever you're storing
   secrets for this project (a password manager) -- it is *not*
   recorded in this repo, on purpose.

6. **FTP-push receiving server.** Plain FTP over `mod_sql`-backed
   virtual users is confirmed working end-to-end (real login + real
   upload, tested from this VM). FTPS/TLS, a dedicated sshd for
   SFTP-push accounts, and nftables rules generated from each
   account's IP whitelist are still not done -- see the end of this
   section.

   Upload root:
   ```bash
   sudo mkdir -p /srv/ftp-uploads
   sudo chown localadmin:localadmin /srv/ftp-uploads
   sudo chmod 755 /srv/ftp-uploads
   ```

   Install ProFTPD + the SQLite and bcrypt/TLS modules (`proftpd-mod-sqlite`
   pulls in `proftpd-core`; `proftpd-mod-crypto` ships `mod_sql_passwd`
   (bcrypt) and `mod_tls` (FTPS), neither is pulled in automatically):
   ```bash
   sudo apt update
   sudo apt install -y proftpd-core proftpd-mod-sqlite proftpd-mod-crypto
   ```

   Enable the SQL modules (commented out by default) -- order matters,
   `mod_sql.c` must load before the others, which is already how
   they're ordered in the stock file:
   ```bash
   sudo sed -i \
     -e 's/^#LoadModule mod_sql\.c/LoadModule mod_sql.c/' \
     -e 's/^#LoadModule mod_sql_sqlite\.c/LoadModule mod_sql_sqlite.c/' \
     -e 's/^#LoadModule mod_sql_passwd\.c/LoadModule mod_sql_passwd.c/' \
     /etc/proftpd/modules.conf
   grep -n "^LoadModule mod_sql" /etc/proftpd/modules.conf   # expect 3 lines
   ```

   Create a dedicated, non-login system account that every virtual
   user maps to (not a real per-provider Linux account -- this is
   `SQLDefaultUID`/`GID`'s target):
   ```bash
   sudo useradd --system --no-create-home --shell /usr/sbin/nologin virtualftp
   id virtualftp   # note the uid/gid, needed below
   ```

   Write the actual SQL config. `/etc/proftpd/conf.d/*.conf` is
   auto-included (`proftpd.conf:210`, already active by default -- no
   edit to the main config needed). Replace `<uid>`/`<gid>` with what
   `id virtualftp` printed:
   ```bash
   sudo tee /etc/proftpd/conf.d/loader-virtual-users.conf > /dev/null <<'EOF'
   SQLBackend sqlite3
   SQLConnectInfo /opt/data-projects/loader-data-gov-gr/data/loader.db
   SQLPasswordEngine on
   SQLAuthTypes bcrypt
   SQLUserInfo ftp_account username password_hash NULL NULL home_directory NULL
   SQLDefaultUID <uid>
   SQLDefaultGID <gid>
   RequireValidShell off
   DefaultRoot ~
   EOF
   sudo proftpd -t   # syntax check only, doesn't start anything
   ```

   **Two real gotchas hit getting this working, both already fixed in
   code/schema, not just worked around by hand:**
   - ProFTPD's `mod_sql` unconditionally queries a table literally
     named `groups` during auth, even with no `SQLGroupInfo` directive
     and no group features used -- `SQLDefaultUID`/`GID` only supply
     *fallback values*, they don't suppress the lookup. Without it,
     every login fails with `mod_sql/4.5: unrecoverable backend
     error: ... no such table: groups`. Fixed by adding an empty
     `FtpGroup` model (`groupname`/`gid`/`members` columns) to
     `models.py` -- created automatically via `Base.metadata.create_all()`
     on any deploy, nothing to do by hand.
   - `bcrypt.gensalt()`'s own default produces a `$2b$`-prefixed hash;
     ProFTPD's `mod_sql_passwd` here silently rejects those (`530
     Login incorrect`, nothing logged as an error) while the identical
     password re-hashed as `$2a$` works. Fixed in `admin_ftp.py`'s
     `_hash_password()` (`bcrypt.gensalt(prefix=b"2a")`) -- any account
     created through the admin API after that fix landed already gets
     a compatible hash; nothing to do by hand for new accounts.

   Enable and start:
   ```bash
   sudo systemctl enable proftpd
   sudo systemctl restart proftpd
   sudo systemctl status proftpd
   sudo tail -30 /var/log/proftpd/proftpd.log   # ProFTPD's own log, NOT journalctl -- connection-level detail lives here, not in the systemd journal
   ```

   Real end-to-end test (create an account via the admin API first,
   see step 5's `curl` pattern, then use its returned username/password):
   ```bash
   curl -T /path/to/local/file "ftp://<username>:<password>@127.0.0.1/<filename>"
   ```

   **FTPS is explicitly out of scope** -- decided against it in favor
   of SFTP, which needs no certificate at all (SSH host keys, not a
   TLS cert -- see the SFTP section below). Still not done: generating
   and applying nftables rules from each account's `allowed_ips`.

7. **SFTP-push receiving**, confirmed working end-to-end (real
   password login + real upload, tested from this VM, same account
   used for the FTP test above -- one set of credentials works over
   either protocol). Uses ProFTPD's own `mod_sftp` rather than a
   separate sshd instance, so it reuses the exact same `mod_sql`
   account table -- no second account system to keep in sync.

   `mod_sftp` is bundled in `proftpd-mod-crypto` (already installed in
   step 6), just needs enabling:
   ```bash
   sudo sed -i 's/^#LoadModule mod_sftp\.c/LoadModule mod_sftp.c/' /etc/proftpd/modules.conf
   grep -n "^LoadModule mod_sftp" /etc/proftpd/modules.conf   # confirm uncommented
   ```

   Generate dedicated SSH host keys for ProFTPD (not the VM's own
   sshd host keys -- these are separate, no certificate involved,
   just a self-generated keypair like any SSH host identity):
   ```bash
   sudo ssh-keygen -t ed25519 -f /etc/proftpd/ssh_host_ed25519_key -N ''
   sudo ssh-keygen -t rsa -b 4096 -f /etc/proftpd/ssh_host_rsa_key -N ''
   sudo chown root:root /etc/proftpd/ssh_host_*_key
   sudo chmod 600 /etc/proftpd/ssh_host_*_key
   ```
   Run each `ssh-keygen` **individually** and confirm its own output
   (`Generating public/private ... key pair` + a fingerprint) before
   moving on -- pasting both as one block silently ran only the first
   one during this project's own setup, with no error, just a missing
   second file caught later by `proftpd -t`.

   SSH/SFTP needs a different port than FTP (port 21) and can't share
   this VM's own admin SSH (port 22) -- ProFTPD requires a separate
   `<VirtualHost>` block for that, on **port 2222**:
   ```bash
   sudo tee /etc/proftpd/conf.d/loader-sftp.conf > /dev/null <<'EOF'
   <VirtualHost 0.0.0.0>
     Port 2222

     <IfModule mod_sftp.c>
       SFTPEngine on
       SFTPHostKey /etc/proftpd/ssh_host_rsa_key
       SFTPHostKey /etc/proftpd/ssh_host_ed25519_key
       SFTPAuthMethods password
       SFTPCompression delayed
     </IfModule>

     SQLBackend sqlite3
     SQLConnectInfo /opt/data-projects/loader-data-gov-gr/data/loader.db
     SQLPasswordEngine on
     SQLAuthTypes bcrypt
     SQLUserInfo ftp_account username password_hash NULL NULL home_directory NULL
     SQLDefaultUID 995
     SQLDefaultGID 985
     RequireValidShell off
     DefaultRoot ~
   EOF
   sudo proftpd -t
   sudo systemctl restart proftpd
   sudo ss -tlnp | grep -E ":21|:2222"   # confirm both listeners are up
   ```
   The `SQL*` directives are repeated here rather than relied on from
   the global config in step 6 -- kept explicit per-VirtualHost so
   there's no ambiguity about what each listener actually uses.
   `mod_sftp` needed no separate auth wiring: it's a protocol frontend
   only, and reuses whatever `AuthOrder`/`mod_sql` config is already
   in effect once `SFTPAuthMethods password` is set (confirmed by
   this actually working on the first real test, not guessed).

   Real end-to-end test, same account as the FTP test:
   ```bash
   curl -T /path/to/local/file "sftp://<username>:<password>@127.0.0.1:2222/<filename>"
   ```

## PostgreSQL (dedicated to this project)

A `postgres:16` container (`loader-postgres`) runs at
`/opt/data-projects/loader-data-gov-gr-postgres` on this VM -- a
sibling directory to this project's own clone, but not inside it (so
`git pull`/`git clean` can never touch its `.env` or runtime state).
Dedicated to this project alone, deliberately -- **not** shared with
`opencar-data-gov-gr` or anything else on this VM. Each project gets
its own isolated database rather than one instance serving several:
a shared instance would couple this project's DB uptime/maintenance to
whoever else came to depend on it, working against the same fault-
isolation/independent-deploy principle this whole service is already
built around (see this README's own rationale for why it talks to
CKAN over its Action API instead of running in-process). If another
project needs Postgres, it gets its own container the same way this
one was set up -- cheap to repeat, no shared blast radius.

Bound to `127.0.0.1:5432` only (never exposed externally, same as
everything else here), resource-capped (`mem_limit: 2g`, `cpus: 1.0`
in its `docker-compose.yml` -- confirmed applied via `docker inspect`,
not just assumed from the compose file syntax: Swarm's
`deploy.resources.limits` block is silently ignored outside Swarm
mode, these top-level keys are the ones that actually work).

**Redeployable, not a one-off**: [`deploy/postgres/docker-compose.yml`](postgres/docker-compose.yml)
is the actual, committed source of truth for the container, and
`.github/workflows/provision-postgres.yml` (manual-dispatch) is
idempotent and safe to leave in the repo permanently: it generates its
own password directly on the VM only the first time it's actually
needed (no GitHub secrets round-trip required), never overwrites an
existing `.env`/database/role, and re-syncs the compose file from this
repo on every run. If the container were ever lost (VM rebuild, disk
issue), re-running that workflow stands it back up from what's
actually committed here.

This service's own database/role:
```
LOADER_HARVEST_DB_URL=postgresql://loader_harvest_app:<password>@127.0.0.1:5432/loader_harvest
```
in `/etc/loader-data-gov-gr.env`, alongside the other secrets --
`loader_harvest_app` can only see the `loader_harvest` database.

## First run

Trigger `Deploy` manually (Actions tab, or `gh workflow run deploy.yml`)
and watch it -- the smoke-test step curls `/health` on the VM itself and
the reload step dumps `journalctl` if the service didn't come back
healthy. Once confirmed, every push to `master` deploys automatically
(this workflow already has the `push` trigger, unlike the earlier
SSH-based draft which was deliberately manual-only).
