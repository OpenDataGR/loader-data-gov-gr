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

5. **Admin API secret**, needed before any `/admin/*` endpoint works
   (they fail-closed with a 503 otherwise). Stored in a separate,
   permission-restricted file rather than inline in the (normally
   world-readable) unit file:
   ```bash
   sudo tee /etc/loader-data-gov-gr.env > /dev/null <<'EOF'
   LOADER_ADMIN_API_KEY=<generate with: python3 -c "import secrets; print(secrets.token_urlsafe(32))">
   LOADER_FTP_UPLOAD_ROOT=/srv/ftp-uploads
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
   The actual key currently in use lives wherever you're storing
   secrets for this project (a password manager) -- it is *not*
   recorded in this repo, on purpose.

6. **FTP-push receiving server** -- in progress, not yet functional.
   So far:
   - Upload root created:
     ```bash
     sudo mkdir -p /srv/ftp-uploads
     sudo chown localadmin:localadmin /srv/ftp-uploads
     sudo chmod 755 /srv/ftp-uploads
     ```
   - ProFTPD installed (`proftpd-mod-sqlite` on Ubuntu 24.04 pulls in
     `proftpd-core` as a dependency; confirmed by actually installing
     it, package version `1.3.8.b+dfsg-1ubuntu0.1`):
     ```bash
     sudo apt update
     sudo apt install -y proftpd-core proftpd-mod-sqlite
     ```
   - **Immediately stopped and disabled** -- its default config isn't
     the SQLite-backed virtual-user setup we want yet, and shouldn't
     be reachable until it is:
     ```bash
     sudo systemctl stop proftpd
     sudo systemctl disable proftpd
     sudo systemctl status proftpd   # confirm: inactive (dead), disabled
     ```
     **Confirm that status output for real before doing anything that
     might reboot the box.** Hit for real on this project: `stop` +
     `disable` were run, but a VM reboot (for an unrelated pending
     kernel update) happened before the `disable` had actually been
     confirmed to take effect, and `proftpd` came back up with default
     config for about 3 minutes after boot before being caught and
     stopped again. Low-risk in this specific case (Ubuntu's default
     proftpd config doesn't allow anonymous login), but the lesson
     generalizes: `systemctl disable` prevents a *future* boot from
     starting a service, it does nothing about one that's already
     running right now -- always pair it with `stop`, and verify both
     landed via `status` before trusting either.
   - **Not yet done**: the actual `mod_sql` config wiring ProFTPD to
     `/opt/data-projects/loader-data-gov-gr/data/loader.db` (the same
     database the admin API writes to), TLS for FTPS, a dedicated sshd
     for SFTP-push accounts, and nftables rules generated from each
     account's IP whitelist. Getting ProFTPD's real expected
     password-hash format right needs verifying against actual
     ProFTPD docs before writing any of this -- in progress.

## First run

Trigger `Deploy` manually (Actions tab, or `gh workflow run deploy.yml`)
and watch it -- the smoke-test step curls `/health` on the VM itself and
the reload step dumps `journalctl` if the service didn't come back
healthy. Once confirmed, every push to `master` deploys automatically
(this workflow already has the `push` trigger, unlike the earlier
SSH-based draft which was deliberately manual-only).
