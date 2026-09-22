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

## First run

Trigger `Deploy` manually (Actions tab, or `gh workflow run deploy.yml`)
and watch it -- the smoke-test step curls `/health` on the VM itself and
the reload step dumps `journalctl` if the service didn't come back
healthy. Once confirmed, every push to `master` deploys automatically
(this workflow already has the `push` trigger, unlike the earlier
SSH-based draft which was deliberately manual-only).
