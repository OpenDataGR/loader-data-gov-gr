# Working on loader-data-gov-gr

External ETL/API-loader for data.gov.gr provider harvesting, replacing
[`ckanext-providerharvest`](https://github.com/OpenDataGR/ckanext-providerharvest)
(a CKAN plugin that never went to production -- development there has
stopped; it's kept only as reference, see "What to reuse" below). Talks
to CKAN entirely over its public Action API, never imports `ckan`/`ckanext`.
Serves [loader.data.gov.gr](https://loader.data.gov.gr).

Read `README.md` first for the full architecture and current status --
this file is oriented at *working* on the project: conventions, where
things are, and what to check before assuming something is broken.

## Layout

- `providerharvest_service/engine/` -- CKAN-agnostic core (transports,
  auth strategies, field-mapping, secrets, SSRF validation). Ported
  verbatim from the old plugin; don't add anything CKAN-aware here.
- `providerharvest_service/ckan_client.py` -- the one deliberate
  integration point with CKAN (`CKANActionClient`, a `get_action`-shaped
  HTTP adapter over the Action API).
- `providerharvest_service/ckan_tokens.py` -- per-user CKAN API token
  provisioning (mint via a sysadmin credential, once, lazily; store
  envelope-encrypted; reuse thereafter).
- `providerharvest_service/auth.py` -- Keycloak JWT validation
  (`require_current_username`). Built and tested against a self-signed
  keypair; real Keycloak client isn't registered yet, so this can't be
  exercised for real over the network yet -- `KEYCLOAK_JWKS_URL`/
  `ISSUER`/`AUDIENCE` are unset in production until that happens.
- `providerharvest_service/admin_ftp.py` + `admin_auth.py` -- the FTP-push
  account admin API (shared-key gated, a deliberate placeholder for real
  per-user auth).
- `providerharvest_service/admin_session.py` -- the login gate for the
  whole human-facing site (signed session cookie, one shared
  username/password), deny-by-default with an explicit exempt list of
  key-authenticated machine endpoints and the public `/proxy/*`. Separate
  from `admin_auth.py`'s shared API keys above. Also a deliberate
  placeholder for the real per-user Keycloak login once `auth.py`'s
  client is registered. A new route that must be reachable without a
  login (a machine caller with its own credential) has to be added to its
  exempt list on purpose.
- `providerharvest_service/db.py` + `models.py` -- SQLite, this
  service's own persistence (not CKAN's DB).
- `deploy/` -- the real, confirmed-working VM setup (systemd, nginx,
  ProFTPD/mod_sql/mod_sftp, the self-hosted GitHub Actions runner).
  Read `deploy/README.md` before touching anything on the VM --
  it documents two real gotchas (an unconditional `groups`-table
  lookup, a bcrypt `$2b$`/`$2a$` mismatch) that look like config bugs
  but are already fixed in code.

## Conventions this project holds itself to

- **Verify ProFTPD/infra behavior against real docs before writing
  config -- don't guess.** Every ProFTPD gotcha hit so far (the
  `groups` table, the bcrypt prefix, `mod_wrap2_sql` not covering
  SFTP) came from something that *looked* like it should work but
  didn't; each was confirmed via cited research before being built
  around, not assumed.
- **Prove the smallest real slice before building on top of it.** The
  deploy pipeline was proven with a bare `/health` endpoint before any
  real route existed; the FTP/SFTP server was proven with one manually
  created test account before the admin API's own tests were trusted;
  token provisioning was proven via `/me` actually calling CKAN as a
  specific person before any real endpoint used it.
- **Tests use fakes, not mocks-of-behavior.** `FakeSession`/`FakeResponse`
  classes (see `test_ckan_client.py`, reused everywhere else) simulate
  the real HTTP contract; JWT tests sign real tokens with a real
  self-signed keypair and exercise the actual signature-verification
  code path, not a stubbed-out approximation of it.
- **The suite runs bcrypt at its minimum cost** (`tests/conftest.py`):
  production-strength hashing in every test's setup was most of a 7-minute
  run. Everything else about the hashing is real, a hash carries its own cost
  so verification works either way, and `test_bcrypt_cost.py` guards that
  production code never sets a cost of its own. Don't hash with a hard-coded
  `rounds=` in the service.
- **Run CI after every unit of work.** Push triggers `deploy.yml`
  automatically (self-hosted runner on `MIN01RG96PVM02`) -- watch it
  land before considering a change done.
- **Secrets go in a `chmod 600` `EnvironmentFile=`, never inline in a
  unit file.** See `deploy/README.md`.

## What to reuse from `ckanext-providerharvest`

The engine is already fully ported -- don't re-port it. What's still
only in the old plugin, and is the actual spec for what's left to
build here:

- `ckanext/providerharvest/logic/action.py` -- registration/approval
  validation rules and state transitions.
- `ckanext/providerharvest/model/` -- schema shapes (`ProviderSource`,
  `FieldMapping`) to model this service's own remaining persistence on.
- `ckanext/providerharvest/harvesters/base_generic.py` -- the real
  transport -> mapping -> loader sequence a scheduled harvest run
  needs (this service needs its own scheduler; `ckanext-harvest`'s job
  model doesn't come along).
- `ckanext/providerharvest/blueprints/provider_ui.py` -- the
  registration form's field-handling/validation logic.
- `ckanext/providerharvest/tests/integration/` -- closest thing to a
  behavioral spec (exact validation errors, exact approval-workflow
  transitions) for not missing something real when porting a feature.

## Shared VM deploy pattern

A `shared-vm-cicd-deploy` skill (in `ksfikas/claude-skills`, not this
repo) documents the general MIN01RG96PVM02 pattern (shared with
`opencar-data-gov-gr`) -- self-hosted runner setup, systemd/nginx
conventions, Azure Application Gateway backend pools. Load it before
touching VM-level deploy config; it has its own Pitfalls section worth
reading first.
