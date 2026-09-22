# providerharvest-service

External ETL/API-loader for data.gov.gr's provider harvesting, replacing
`ckanext-providerharvest`'s in-process CKAN plugin design. This talks to
CKAN entirely over its public Action API (using per-user CKAN API tokens
-- see the design discussion this split came out of), not by running
inside CKAN's own process/venv. That buys deploy independence (no CKAN
restart/plugin-reload to ship a change here), fault isolation (a bad
harvest run can't take the portal down), and a smaller blast radius for
outbound calls to arbitrary provider-supplied endpoints, at the cost of
losing atomic DB transactions across a harvest job + its CKAN-side
writes, and needing real API-token lifecycle management instead of
implicit in-process trust.

Currently lives as a directory in the same repo as the CKAN plugin
while the split is being worked out; the intent is to extract it into
its own repository once the shape settles. Do not add a dependency from
here back onto `ckanext.providerharvest` (or vice versa) -- the whole
point is these become independently deployable.

## Status: engine + CKAN adapter (no persistence/API/CLI yet)

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

Not yet built, in the rough order they'll likely be needed:
- This service's own persistence (provider-source config, approval
  status, field-mapping profiles, encrypted credentials) -- it no
  longer has CKAN's DB/SQLAlchemy models to lean on.
- The token-provisioning flow itself: minting a per-user CKAN API token
  via the standing sysadmin credential the first time a user needs one
  (see the design discussion -- lazily, not at every login), and
  storing it via `engine/secrets` (the same envelope-encryption already
  built for provider credentials).
- A web framework layer (endpoints for register/test-connection/
  list/approve/reject/pause/resume) and its own auth middleware
  (validating the caller's own identity and looking up their stored
  per-user token to construct a `CKANActionClient`).
- Its own scheduler/queue for running harvests -- `ckanext-harvest`'s
  gather/fetch/import machinery doesn't come along; this service owns
  that job-queue concern itself now.
- Its own `docker-compose.yml`, since "independently deployable" means
  not piggybacking on `docker/`'s CKAN replica.

## Running the engine's tests

```bash
cd service
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows; drop the .exe/Scripts on Linux/macOS
./.venv/Scripts/python.exe -m pytest tests
```

No CKAN installation, no Docker, no database -- this is the whole point
of the split being checked for real, not just asserted. 107 tests,
all passing with zero CKAN present.
