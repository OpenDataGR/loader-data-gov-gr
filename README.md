# loader-data-gov-gr

External ETL/API-loader for data.gov.gr's provider harvesting -- serves
[loader.data.gov.gr](https://loader.data.gov.gr) -- replacing
[`ckanext-providerharvest`](https://github.com/OpenDataGR/ckanext-providerharvest)'s
in-process CKAN plugin design. This talks to CKAN entirely over its
public Action API (using per-user CKAN API tokens -- see that repo's
design discussion for the rationale), not by running inside CKAN's own
process/venv. That buys deploy independence (no CKAN restart/
plugin-reload to ship a change here), fault isolation (a bad harvest
run can't take the portal down), and a smaller blast radius for
outbound calls to arbitrary provider-supplied endpoints, at the cost of
losing atomic DB transactions across a harvest job + its CKAN-side
writes, and needing real API-token lifecycle management instead of
implicit in-process trust.

Split out of `ckanext-providerharvest`'s `service/` directory (with
full git history preserved via `git subtree split`) once the shape
settled enough to be worth its own repo, own CI, and own deploy
pipeline. Do not add a dependency from here back onto
`ckanext.providerharvest` (or vice versa) -- the whole point is these
are independently deployable.

## Status: engine + CKAN adapter + a bare health check + CI (CD scaffolded, needs secrets)

`providerharvest_service/engine/` -- the transport implementations
(HTTP/SFTP/SCP/FTP), the HTTP auth strategies (API key/Basic/OAuth2
client-credentials/mTLS), envelope-encrypted secret handling,
field-mapping/extraction, SSRF-safe network-target validation, response
parsers, and the CKAN-DataStore/resource loaders -- **ported, not
rewritten**, from `ckanext/providerharvest/`'s own copies of these
modules. That plugin's engine code was already CKAN-agnostic (no `ckan`
import anywhere in transport/, auth_strategies/, secrets/, mapping.py,
or logic/validators.py -- only sibling `ckanext.providerharvest.*`
imports), which is what makes this a port instead of a rewrite: import
paths were rewritten to this package's own namespace and nothing else
changed. `loaders/*.py` were already dependency-injected against a
`get_action`-shaped callable rather than importing CKAN's `toolkit`
directly, so they carry over unchanged too.

`providerharvest_service/ckan_client.py` -- the one deliberate
integration point with CKAN: `CKANActionClient` implements that
`get_action`-shaped adapter over `requests` + a bearer token, matching
`toolkit.get_action`'s own call signature so the ported loaders work
against it with zero changes (checked directly, not just asserted --
see `test_datastore_loader_works_unchanged_against_this_client` in
`tests/test_ckan_client.py`). One instance is bound to exactly one CKAN
API token/identity, per the "per-user token does both" design: the same
per-user token drives both the `organization_list_for_user`
authorization check and every subsequent write, so CKAN's own
permission system enforces org membership for real on every call.
Nothing under `engine/` imports this module or knows CKAN exists.

`providerharvest_service/app.py` -- a FastAPI app with exactly one
route, `GET /health`. Deliberately trivial: a deploy pipeline needs
*something real* to push end-to-end (systemd unit, self-hosted runner,
through the Application Gateway, a live response at
`loader.data.gov.gr/health`) before any real endpoint gets built on top
of infrastructure nobody's verified yet.

`.github/workflows/tests.yml` -- runs the test suite on a GitHub-hosted
runner on every push/PR, no CKAN/Docker involved (an independent check,
separate from the deploy pipeline's own test gate below).
`.github/workflows/deploy.yml` -- follows this Ministry's established
shared-VM pattern (first built for `opencar-data-gov-gr`, confirmed
against that project's real working config on `MIN01RG96PVM02` before
writing this one): a GitHub Actions **self-hosted runner installed
directly on the target VM** (Azure Bastion doesn't expose it for plain
SSH from a GitHub-hosted runner, so this sidesteps that entirely --
deploy is local filesystem + systemd operations, nothing crosses the
network), running as the `localadmin` service account against a
persistent clone at `/opt/data-projects/loader-data-gov-gr`, gunicorn
+ UvicornWorker on port 5002 (5001 is `opencar-data-gov-gr`'s) under
systemd, restarted via `pkill` + `Restart=always` rather than
`sudo systemctl restart` (this VM's sudoers needs a password for
arbitrary sudo, which would hang in CI). See
[`deploy/README.md`](deploy/README.md) for the full one-time VM setup.
Already wired for `push: { branches: [master] }` -- every merge to
`master` deploys automatically once the one-time VM setup is done.

`providerharvest_service/db.py` + `models.py` -- this service's first
real persistence, a SQLite file (`LOADER_DB_PATH`, defaults under
`data/`, gitignored, survives redeploys since it lives inside the
persistent clone and `git pull` never touches untracked files).
`admin_ftp.py` -- the first real admin-panel feature: CRUD for
FTP-push accounts and their per-account IP whitelists, gated by
`admin_auth.py`'s shared admin-key dependency (`LOADER_ADMIN_API_KEY`
header `X-Admin-Key` -- a deliberate placeholder for the real
per-user Keycloak/CKAN-token authorization design, swapped in once
the token-provisioning flow below exists). `home_directory` is
validated to sit under `LOADER_FTP_UPLOAD_ROOT` (default
`/srv/ftp-uploads`) so the admin API can't be pointed at an arbitrary
VM path. Generated passwords are bcrypt-hashed and shown exactly once,
at creation (or rotation) -- never returned again.

**Deliberately not yet in this slice**: syncing this data into a
running ProFTPD (or a dedicated sshd for SFTP) and generating the
nftables rules the IP whitelist implies. Getting a real server's
expected password-hash format right needs verifying against actual
ProFTPD docs rather than guessing at it -- that's its own next piece,
not bundled into the API/data-model slice that's already tested and
working.

Not yet built, in the rough order they'll likely be needed:
- Syncing FTP accounts into a real ProFTPD (+ a dedicated sshd for
  SFTP) and generating nftables rules for the IP whitelist.
- The rest of this service's own persistence (provider-source config,
  approval status, field-mapping profiles, encrypted credentials).
- The token-provisioning flow itself: minting a per-user CKAN API token
  via the standing sysadmin credential the first time a user needs one
  (lazily, not at every login), and storing it via `engine/secrets`
  (the same envelope-encryption already built for provider credentials).
- The real endpoints (register/test-connection/list/approve/reject/
  pause/resume) and their own auth middleware (validating the caller's
  identity and looking up their stored per-user token to construct a
  `CKANActionClient`).
- Its own scheduler/queue for running harvests -- `ckanext-harvest`'s
  gather/fetch/import machinery doesn't come along; this service owns
  that job-queue concern itself now.

## Running the tests

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows; drop the .exe/Scripts on Linux/macOS
./.venv/Scripts/python.exe -m pytest tests
```

No CKAN installation, no Docker, no database -- this is the whole point
of the split being checked for real, not just asserted. 108 tests,
all passing with zero CKAN present.

## Running the app locally

```bash
./.venv/Scripts/python.exe -m providerharvest_service.app   # or: uvicorn providerharvest_service.app:app --reload
curl http://127.0.0.1:8000/health
```
