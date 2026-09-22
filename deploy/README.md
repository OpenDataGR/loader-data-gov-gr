# Deploying to the target VM

The target VM sits behind Azure Bastion, which doesn't expose it for
plain SSH from a GitHub-hosted runner. Instead of tunneling (Azure CLI
+ `az network bastion tunnel` + a service principal), this installs a
GitHub Actions **self-hosted runner directly on the VM** -- deploy
becomes local filesystem + systemd operations, nothing needs to reach
the VM over the network at all.

## One-time setup, run on the VM itself (via your own Bastion session)

1. **Create the deploy user**, if it doesn't already exist:
   ```bash
   sudo useradd -m -s /bin/bash providerharvest
   ```

2. **Create the persistent deploy directory**, owned by that user
   (separate from the runner's own ephemeral checkout workspace):
   ```bash
   sudo mkdir -p /opt/loader-data-gov-gr
   sudo chown providerharvest:providerharvest /opt/loader-data-gov-gr
   ```

3. **Register the self-hosted runner.** On this repo:
   `Settings -> Actions -> Runners -> New self-hosted runner`, choose
   Linux, and follow the download/configure commands shown there
   (they're version-pinned per session, don't copy stale ones from
   elsewhere). When `config.sh` asks for labels, add `loader-vm` --
   `deploy.yml` targets that label specifically, not any self-hosted
   runner that might get added later for something else.

   Install it as a service running **as the `providerharvest` user**
   (matching `providerharvest-service.service`'s `User=`), not root:
   ```bash
   sudo ./svc.sh install providerharvest
   sudo ./svc.sh start
   ```

4. **Install the systemd unit**:
   ```bash
   sudo cp deploy/providerharvest-service.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable providerharvest-service
   ```
   (The unit won't start cleanly until the first deploy run has
   installed the package into `/opt/loader-data-gov-gr/.venv` --
   that's fine, `deploy.yml`'s restart step handles the first start
   too.)

5. **Grant exactly one passwordless sudo rule**, nothing broader --
   `/etc/sudoers.d/providerharvest-deploy`:
   ```
   providerharvest ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart providerharvest-service
   ```
   Validate with `sudo visudo -c` before trusting it.

## Confirm the reverse proxy / port

`deploy/providerharvest-service.service` binds the app to
`127.0.0.1:8000` by default (`PROVIDERHARVEST_PORT`). Whatever already
routes `Host: loader.data.gov.gr` traffic on this VM (the Application
Gateway backend pool forwards here) needs to point at that same port --
confirm this against the actual reverse proxy config before the first
real deploy, and change the unit's `Environment=PROVIDERHARVEST_PORT=`
line (and this note) if it needs to be different.

## First run

Trigger `Deploy` manually from the Actions tab (or `gh workflow run
deploy.yml`) and watch it -- the last step curls `/health` on the VM
itself and fails loudly with the service's recent logs if it didn't
come up clean. Once that's confirmed working, add a
`push: { branches: [master] }` trigger to `deploy.yml` to auto-deploy
on every merge to `master`.
