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

## The shape: Extract, Transform, Load

The service follows an ETL flow, and its tabs follow it: sources come **in**, may be **transformed**, and go **out**
(to data.gov.gr, or by other means).

| Tab | Role | State |
|---|---|---|
| **Extract Box** | the sources: registering them, how they deliver (pull over HTTP/SFTP/FTP, or push by API key or FTP/SFTP login), their runs and files | built (this was "Provider portal") |
| **Transform Box** | turning what was extracted into what is wanted: read the format, add columns from an index table, filter, rename, convert, output | built (first slice, below) |
| **Load Box** | sending the result out to data.gov.gr or elsewhere | first slice built (published API address, below); CKAN push planned |
| **Data boxes**, Security box (super admins) | operating it: the HTTP and FTP machines; access control | built |

Only the tab is renamed for now; the addresses (`/provider/...`) are unchanged so existing links and the providers' own
bookmarks keep working.

## Status: engine + CKAN adapter + working CI/CD + a live FTP/SFTP receiving server + token provisioning + full provider/admin source lifecycle (register/edit/delete) + connection health checks + a live API proxy + a real, manually-triggered harvest that stores data in PostgreSQL

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
validated to sit under `LOADER_FTP_UPLOAD_ROOT` (default `/srv/ftp`,
the external FTP-receiving box's own upload root -- see
`ftp-forwarder/README.md`; this VM's own `/srv/ftp-uploads` was only
ever a proof-of-concept, see `deploy/README.md`'s FTP-push section) so
the admin API can't be pointed at an arbitrary path. Generated
passwords are bcrypt-hashed and shown exactly once, at creation (or
rotation) -- never returned again.

The whole site -- the Extract Box (the provider-facing pages), the admin pages, the API docs --
sits behind a real login (`admin_session.py`: a signed session cookie,
one shared username/password, enforced by a single ASGI middleware). It
is **deny by default**: a path needs the session unless it is on a short
explicit list of things that aren't a person in a browser and carry their
own credential -- `/health`, `/push/*` (API key), the FTP box's
`/admin/ftp-relay/*`, `/admin/ftp-accounts-sync`, `/admin/ftp-box-status` and
`/admin/ftp-login-attempts` (sync key; the last feeds the failed-login report
at `/admin/security/failed-logins`, see `ftp-forwarder/README.md`), the JSON admin API `/admin/ftp-accounts*` and
`/admin/harvest-db/health` (admin key), `/me` (bearer token) -- plus
`/data/*`, the published pipelines (and the live open-data answers of API sources), which are public by design. A route
added later is therefore protected without anyone remembering to. (Until
this it was the other way round -- only `/admin/sources` and `/admin/ftp`
were gated, which left the provider portal, and the harvested files it
serves, open to anyone.) All of it is a placeholder, for the same reason
the shared admin API key is: the real design is per-user Keycloak login,
not built yet.

A source is either **pull** (we fetch it -- HTTP/SFTP/SCP/FTP, on a
harvest frequency) or **push** (the provider sends it as it arrives, no
schedule -- `transport_type="push"`, with `config.push_method`
`https` for an API key or `ftp` for a login on the FTP/SFTP box). A
provider registering an `ftp` push source picks (or has generated) a
username and password on the registration form itself
(`provider_sources.register`); the account is created disabled and
tied to the source (`FtpAccount.managed_source_id`), so approving the
source enables the login and pausing/rejecting/deleting it disables or
removes it. The credentials page shows the server
(`LOADER_FTP_PUBLIC_HOST`) and ports once; the password is never stored
in recoverable form. A push source's row then shows where its data comes
from and when the last file arrived (`ProviderSource.last_sender`,
`last_received_at`): for an HTTPS push, the sender's address taken from
the `X-Forwarded-For` chain (`client_addr.py` -- the gateway's view of the
client, not anything the client claims); for FTP/SFTP, the address the FTP
box last saw that provider log in from, carried in its heartbeat (loader
never talks to the provider directly on that path).

**Allowed senders.** A push source may be limited to its provider's own
machine(s): the registration (and edit) form takes an optional list of IPv4 or IPv6
addresses, ranges (CIDR) or hostnames/URLs, stored in
`config.allowed_senders` as canonical CIDRs (`sender_allowlist.py`).
Everything else is refused, even with the right key or password; blank means
anyone. A hostname is looked up once, when the form is saved, and the
addresses it points to are stored (not the name: checking a name per request
would put DNS on the upload path and hand the decision to whoever controls
that record) -- so a sender whose address changes must update the source.
Enforced where the data arrives: the HTTPS push endpoint checks the sender
address (the trusted `X-Forwarded-For` entry, as above; an address that cannot
be determined is refused) *before* it looks at the API key, answers 403, and
records the refusal in the Security box report as *Push: address not allowed*;
for an FTP/SFTP source the list is copied onto its account
(`FtpAllowedIp`, rewritten whenever the source is saved) and the FTP box turns
it into ProFTPD login rules (see `ftp-forwarder/README.md`, "Per-account login
limits"). The source is the single place the list is edited: the FTP box page
shows a provider's account list read-only, and the JSON admin API refuses to
change it (409). Admin-made accounts keep their manual "Allowed from" list.

**Credentials a provider cannot recover.** An FTP/SFTP password is stored only as
a bcrypt hash (`$2a$`, the prefix ProFTPD needs) and an HTTPS push key likewise, so
neither can ever be displayed -- not even a password the provider chose at
registration; making them viewable would mean storing them recoverably, and a
leaked database would then hand over every provider's password. What an admin can
do instead -- all on the source's **Credentials page**
(`/provider/sources/{id}/credentials`, linked from the "..." menu of every push
source in the portal and on the Extract Box; one home for a push source's
login or API key, its allowed senders and its last activity, with no copies of
those controls elsewhere: the FTP box page shows a provider's own account
read-only with a "Manage on the source" link, and only accounts an admin made by
hand are changed from that page): **Set a password**
(`/admin/ftp/{id}/set-password`: one the provider gave you; at least 12 characters,
at most 72 bytes -- bcrypt reads no more, so a longer one would silently match any
ending -- typed twice, never echoed or put in a URL; the same rules now apply to a
password chosen at registration), **Rotate** to a generated one (shown once; asks
for confirmation, since it replaces the current password at once and breaks the
provider's uploads until they switch), and for an HTTPS push source **Rotate API
key** (`/provider/sources/{id}/rotate-key`: the old key stops working immediately;
before this a lost key meant deleting and re-registering the source).

**Input and Output are steps** (Transform Box): a transform's list reads **IN** -> the processing steps (the first is *Read XML/JSON/CSV/Excel*) -> **OUT**. How callers ask an API source's published address (the GET->POST turn-around and its template) is set in the Load Box, on the source's row (Request -> Edit). A source can have several pipelines (each with its own steps, output and on or off switch; a new one is added from the Transform Box page, empty or as a copy of one of the source's pipelines): the Transform page has a switcher, and a pipeline's pages live under /transform/sources/<id>/pipelines/<name>/. The first is named default. Every enabled pipeline runs on an arriving file and stores its own output. Index tables belong to one source (Extract Box, the source's menu: Index tables): uploaded, replaced and deleted there, and only that source's pipelines can look things up in them; a table name is unique within its source, never shared. Deleting a source deletes everything that belongs to it: its pipelines, index tables, publish setting, stored runs, files and outputs. The source form (Extract Box, register and edit) is split into tabs -- General (name, title, e-mail), Connection (pull or push, the connector, endpoint, schedule and credentials), Request and Limits (every rate limit, timeout, retry and depth setting together) -- and only the tabs a source needs are shown: a push source and SFTP / FTP have General and Connection (a push source's how-they-send and allowed senders are its Connection), an HTTP API all four. On a new source the button leads through the tabs ("Next: Connection", ...) and becomes Register source once every required field is filled. The Recent outputs table of a pipeline has checkboxes: download the selected outputs as one zip, delete them, or delete every failed output of the pipeline in one click.
The two ends are fixed (not removable or movable, not offered under "Add a step") and each has its own page, opened by its Edit button:
*Input* (`/transform/sources/{id}/steps/input`) says where the data comes from -- for a source that stores data, the newest file the steps are built
on; for an API source, its sample answer (paste one, or fetch one with the source's test request); *Output* (`.../steps/output`) holds the format and
its settings (JSON incl. "one object when there is one record", JSON Lines, XML names, CSV separator and Excel mark). Their cards show a one-line summary
in words. The settings still live in the `Transform` row, so nothing was migrated.

**Publishing: the one switch** (`publishing.py`, `load_api.py`, the Load Box): what is published is a **pipeline** (`Transform.published`). An admin
switches **Publish** on for it, and it answers at `/data/<source>/<pipeline>` -- the source's `default` pipeline also at `/data/<source>` -- public
and without a login, only while its source is active. A pipeline of a source that stores data answers with its newest good output (GET). A pipeline
of an **API source** (HTTP) stores nothing: the provider is asked *at the moment someone calls* -- with the source's credential and the pipeline's
request settings and steps -- and takes a GET or a POST depending on the pipeline's request settings (below); a pipeline that is switched off, or has
no steps, passes the provider's answer through unchanged. Several pipelines on one source each have their own address, their own Publish switch and
their own request settings, and removing a pipeline stops its address. There is no separate "live proxy" and no `/proxy/*` address any more.
`publishing.migrate_to_pipelines` carried the old per-source switch over once (the source's default pipeline is published, created without steps for an
API source that had none), so nothing that answered stopped answering; a pipeline created later is **not** published until someone switches it on.
Where the sections below say "live proxy" or `/proxy/`, read "published address" and `/data/`.

**POST APIs** (`proxy.py`, `proxy_method`): an HTTP source can be registered with the method `POST`. Its live proxy
(`POST /proxy/{name}`) then passes the caller's JSON body (application/json only, valid JSON, at most 64 kB) to the provider's
endpoint, with the provider's credential added by the loader (a caller's own `Authorization` or other headers are never sent), and
relays the answer. One method per source: a POST source answers `405` to GET and an ordinary source answers `405` to POST. Such a
source is **proxy-only**: it is registered with frequency MANUAL, never pulled on a schedule and "Harvest now" is refused. Whether it is exposed
is the form's "also expose this as a live proxy" box, like any source's: unticked, nobody can call it. Its connection
check never POSTs (each call to a question-per-call API is a real request and may create something at the provider): it sends one harmless GET
with the credential and reads the answer -- any answer shows the address, the network path and the certificate work (a POST-only API
answers 405, as expected), 401/403 means the credential may be refused, 404 or 5xx and connection errors are errors; nothing is created.
The credential and the request are only proven by a real call, which the Transform page's "Fetch a sample" makes on purpose. The per-source
request timeout (`request_timeout_s`) should fit a slow API, and any gateway in front of the loader needs a timeout at least as long
(an Azure Application Gateway's backend setting defaults to 20 s). The proxy is public by design, like every `/proxy/*`: put nothing
behind it that must not be reachable by whoever knows the source name.

**Transform on live answers** (`proxy._reshape`, `Transform.sample`, `Transform.output_single`): an API source (HTTP, with a live
proxy) can have a transform too. A successful (2xx) answer of the provider is run through the source's switched-on transform before it
leaves the loader -- typically one *read JSON* step that keeps only chosen fields -- so the other fields never leave. It **fails
closed**: when the transform cannot be applied (the answer is not JSON, the fields are not there) the caller gets `502 the provider's
answer could not be reshaped`, never the unreshaped answer; an error from the provider is passed on as it is; with no transform, or
one that is switched off, the answer passes through as before. An API's answers are not stored, so the steps are built and tried on a
**sample answer** an admin pastes on the source's Transform page (JSON, up to 200 kB, kept in the config database, never used to
answer anyone; use test data where possible). *When there is exactly one record, return that object instead of a list* (JSON output,
on by default for an API source) makes a lookup answer one object. Nothing is stored, so those sources have no "run again" or "recent
outputs"; the Load Box says "pass-through, reshaped by the transform". Instead of pasting, the sample card can **fetch one from the provider**
(`POST /transform/sources/{id}/fetch-sample`, `proxy.call_provider`): one real call with a test request the admin gives (the values of the
request template's placeholders, a JSON body, or query parameters), the source's own credential, timeout and rate limit, confirmed in the
browser first because it may create a record at the provider; the answer (2xx, JSON, up to 200 kB) becomes the sample and the page goes
straight to choosing the fields. The provider's content is never echoed in an error message.

**Request turn-around** (`request_template.py`, `Transform.request_swap` / `request_template`, the *Request* card on an API source's Transform
page): the live proxy can accept the *other* method than the provider uses. Provider `POST` (the source's setting), callers `GET`: the
caller's query parameters fill `{name}` placeholders in a JSON body template (`/proxy/registry?plate=AB1234` becomes the registry's
POST body). Provider `GET`, callers `POST`: fields of the caller's JSON body (`{a/b}` for nested ones) fill a `name={field}` query
template. Only what the template names is passed on (other parameters and fields are dropped); a value is escaped (JSON-escaped into
the body, URL-encoded into the query), limited to 200 characters and refused if it holds a control character, so it cannot change the
structure of what the provider receives; a template whose body is not valid JSON with a dummy value in each placeholder is refused when
it is saved. A call that does not fit gets a `400` naming the parameter, never its value; the wrong method gets `405` with `Allow`. The
provider's own credential is added as usual and wins over anything a caller supplies. The Load Box shows the call as callers make it
(`GET /proxy/registry?plate=...`, "sent to the provider as POST"). It works together with the response transform above. Mind that a GET
puts the value in the URL, so it appears in access logs.

**Test request** (`ProviderSource.test_request`, the *Test request* field of an API source's form): the request to use when testing this API --
the JSON body a POST API expects, or the query string of a GET one -- with a test case in it. When it is filled, *Test connection* makes **one
real call** with it (the strongest check: address, credential and request; for a POST that may create a record at the provider, so the button asks
first) and reports by status: 2xx reachable, 401/403 credential refused, 404 address not found, other 4xx the test request refused, 5xx a server
error; nothing the provider sent back is repeated. When it is empty, a POST API is checked with one harmless GET and any other API the usual way.
The same request is what the Transform page's *Fetch a sample* sends (see above), so it is only ever written once, on the source.

**Login** (`/admin/login`): the site sits behind a session; the session is started either by the shared username/password (the original placeholder) or, once configured, by the data.gov.gr Keycloak (`oidc_login.py`: authorization code + PKCE, signature/issuer/audience/nonce checks, role `loader-admin` required) -- setup in `deploy/README.md`; with `LOADER_OIDC_ACCESS=ckan-org-admin` the rule is CKAN's (a sysadmin or an administrator of an organization, asked of CKAN with the loader's sysadmin key). The Data boxes and Security box are for super admins only (a CKAN sysadmin, the Keycloak role `loader-superadmin`, or the shared break-glass account). The person's own Keycloak user name then appears everywhere the shared name did.

**Readiness check for a push source** (`push_check.py`, `GET /provider/sources/{id}/readiness`, "Check readiness" in the source's menu): the "test
connection" of a source the provider sends to us. There is nothing the loader can connect to (the FTP box only calls out), so it lists as ticks,
warnings and crosses what has to be true for an upload to arrive, from what the loader knows: the source is active; (FTP/SFTP) the login account
exists, is enabled and forwards to this source; the FTP box reported within the last 5 minutes, ProFTPD runs and listens on 21 and 2222, the relay
runs, its last account sync worked and it holds as many accounts as the loader has enabled; (HTTPS) an API key is set; the sender list; and
whether a file has ever arrived. The summary names the worst problem first. It cannot prove that a login from outside works -- only a file
arriving does, and the last item says so.
The result is also the source's **health** (like Test connection's, with the date): every check green -> *reachable*, any cross -> *unreachable* (the tooltip names
the worst problem), only warnings (for example no file has arrived yet) -> an amber *warning*.

**Integrity of what arrives** (FTP/SFTP push): the box's catch-up sweep skips a file that is still being written; the box sends a SHA-256 and the loader refuses a mismatch and answers with the hash of what it stored (a transfer counts as done only when both agree); a provider may also send `<file>.sha256` before each file for an end-to-end check, and a file that does not match it is put aside on the box instead of sent; each run's detail page shows the stored file's SHA-256. Details and a ready-to-send provider note: `ftp-forwarder/README.md`.

**Load Box** (`/load`; `load_api.py`, `ui_load.py`). One row per pipeline; an admin can *publish* its transformed output: `GET /data/{source name}/{pipeline}` (the `default` pipeline also `/data/{source name}`, and a trailing file name is ignored, for tools that want an extension) then answers, to anyone and without a login, with the newest successfully transformed output that still has its contents, in its own content type, with an `ETag` (conditional requests get 304) and a 60 s cache hint. Off by default; only for an active source; an unknown, unpublished, paused or empty source is the same 404; per-address limit of 60/min (shared limiter with `/proxy`). The planned next destination is a CKAN resource that links to this address, pushed automatically after each transform and by hand.

**CKAN destination** (`ckan_load.py`, `ui_ckan.py`, `CkanTarget`; the Load Box's *Push to CKAN* link, `/load/sources/<id>[/pipelines/<name>]/ckan`). A pipeline can be given a **resource** in an existing dataset of data.gov.gr, in one of two ways chosen per pipeline: **link** (the resource's URL is the pipeline's published address, so it always points at the current data; the pipeline must be published; the only way for an API source) or **upload** (the newest output is uploaded into CKAN's own storage, and again every time a new one is made; a source that stores data). The flow is organization -> dataset -> resource: the person first picks an organization (searchable; those they can create datasets in, an editor included, a sysadmin all of them), then one of that organization's datasets (searched within it; the dataset must belong to the chosen organization), then names the resource. Everything is done **as that person**: their CKAN user `user_<Keycloak sub>` (the `sub` rides in the signed profile cookie) with their own CKAN API token, minted once through the sysadmin credential (`ckan_tokens`), so CKAN's own permissions decide -- a session without a CKAN identity (the shared admin password) cannot use it, and `CKAN_BASE_URL` / `CKAN_SYSADMIN_API_TOKEN` / `LOADER_PUBLIC_BASE_URL` must be set. The person who connected a pipeline is kept on the connection and later changes are made as them. CKAN follows the pipeline, best effort (CKAN being down or saying no never blocks the loader; what went wrong is kept on the connection and shown): a new output updates an `upload` resource; unpublishing removes the resource and publishing again puts it back; removing a pipeline, or deleting a source, takes its resources out of CKAN, and when CKAN refuses the message says to remove the resource there by hand. Moving a pipeline to another dataset removes the old resource first. A third choice, **a data service**, describes the endpoint in data.gov.gr's data-services catalogue (a CKAN dataset of type `data-service`, `/data-service/` on the portal; it has no resources of its own): the entry's endpoint is the pipeline's published address and its endpoint description is written from the pipeline (method, the parameters of the request template, what it answers, the per-caller limit), while licence, access rights, themes, applicable legislation, high-value-dataset category, contact and publisher are chosen on the page from the EU vocabularies (`ckan_vocab.py`) and remembered per organization (`CkanServiceDefaults`), so the next service of the same organization starts from the same values. The format is never chosen by hand: it is the pipeline's output format (JSON for a pipeline that is switched off, which passes an API's answer through), shown read-only, and the entry in CKAN is updated when the pipeline's output or its on/off switch changes. It is the default for an API source. It follows the pipeline like a resource does: unpublishing deletes the entry and publishing again creates it (CKAN keeps a deleted entry's address reserved, so the new one takes the next free one, `-2`), and switching between a resource and a service, or to another organization, removes what was in CKAN first.

**Transform Box** (`/transform`; `transform_engine.py`, `transform_forms.py`, `transform_runner.py`, `ui_transform.py`). A source's
transform is an ordered list of steps run on every file it receives (and re-runnable over earlier ones), producing JSON, JSON Lines,
XML or CSV stored beside the original -- which is never changed. It is built to be generic: step 1 is a **reader** for the file's
format (XML, JSON, CSV or Excel .xlsx, picked from a real stored file: the page finds the repeating records and their fields, so choosing them is
clicking, not typing paths; XML is read by element name, ignoring namespaces and prefixes, and a repeated part inside a record can be
turned into one row per repeat), and every later step works on the same flat records whatever the format: **lookup** (add columns from
an uploaded **index table** (Excel .xlsx, CSV or JSON; for Excel the first row is the header, the first sheet with data is used unless one is named, dates and whole numbers are read cleanly, and a zip-bomb or damaged file is refused), e.g. a station id -> its name and location, with a choice of what to do when there is no match),
**filter**, **rename**, **convert** (int, float, bool, string, datetime to ISO UTC), **keep/remove fields**, **add a field** (constant
or built from other fields), **UTM to latitude/longitude** (WGS84 UTM easting/northing, e.g. zone 34 or 35 for Greece, into degrees; pure-Python, no projection library; bad values become empty and are counted). A new format or step is one more entry in `READERS` / `STEPS`. The page offers **Try on the latest file**
(runs the steps and shows counts, the first rows and which ids found no match; stores nothing), switch on/off, **Run it again on earlier
files** (newest 10/50/200, with a time limit), and downloads of the outputs. A transform that stops holding together (a step uses a field
no earlier step produces, an index table or column is gone) is refused or switched off with the reason. Safety: provider XML is parsed
with `defusedxml` (DTDs and entities refused), steps are data not code, and input, record, step and index-table sizes are capped. A
failure leaves an error row and never blocks the file from arriving. Outputs (`TransformedFile`, harvest database) follow the source's
loader retention: contents emptied, row kept. Only file inputs are transformed so far (pushed files; HTTP-source records are not).

**Runs page.** A source's Runs page (`/provider/sources/{id}/runs`) pages through its whole history
(it used to show only the newest 20): 25, 50 or 100 per page, newest first, with the total ("Showing 26-50 of
1,691 runs"), a status filter, a UTC date range (inclusive; a reversed range is swapped) and Newer/Older links that keep the
filters. A File column shows each run's file name and size, and marks one whose contents retention removed. Anything in the
address that is not an offered value falls back to the default and is never echoed back.

**Data retention per source** (`retention.py`; Extract Box, for a super admin -> a source's "..." menu -> *Data retention*). Two
fixed-choice dropdowns on each source: how long the **loader** keeps a received file's contents (7, 30, 90, 180, 365
days or forever; default 90) and, for an FTP push source, how long the **FTP box** keeps its archive of forwarded
files (1, 7, 14, 30, 90 days or forever; default 30). Stored in the source's `config`; an edit keeps it. On the loader
an hourly thread in each worker empties the *contents* of files past the period -- the run, and the file's name, type,
size and time, stay, and the Runs page shows the file as "removed (retention)" (download answers 410). Only whole files
are purged, not an HTTP source's small JSON records. Shortening a period deletes data that cannot return, so the page
previews what would go ("412 files, 173 MB") and asks first; if the stored data cannot be read it refuses rather than
delete blind. The box is told each account's number in the account sync (`archive_days`) and deletes itself
(`ftp-forwarder/archive_cleanup.py`). `LOADER_RETENTION=0` turns the loader's purge off.

**Data boxes: resources and actions.** The *Data boxes* tab (`/admin/ftp`; it was "FTP box")
shows the **HTTP box** -- loader itself -- and each FTP box, with each box's own figures
(load, memory, disk, its addresses, network traffic in and out -- a rate between two reports
plus totals since boot -- uptime, open sessions; an FTP box reports them in the heartbeat,
stored as `FtpBoxStatus.resources`, and the HTTP box is read live from `/proc`). The two panels
refresh themselves every 30 seconds unless someone has output open and a "..." menu of **actions**: restart ProFTPD,
restart the file relay, reload fail2ban, run diagnostics (a read-only snapshot with
recent log lines). Loader cannot reach a box, so a request (`FtpBoxCommand`, with who
asked and what the box answered, kept 30 days) waits until the box's next heartbeat
collects it -- about a minute -- and the answer returns in the one after. The box runs
only what is on **its own** fixed list (loader sends an action name, never a command
line); each request is handed over at most once, and one nobody collects in 15 minutes
expires. A disruptive action asks for confirmation.
On the **HTTP box** (`http_box.py`) the actions run here, now: *Run diagnostics*; *Reload workers*
(`SIGHUP` to the gunicorn master: workers are replaced one by one, nothing is dropped, safe any
time); and *Restart the whole service* (`SIGTERM` to the master, as a deploy does -- systemd starts
it again, and ALL of loader is unavailable for ~15 s, including the FTP boxes' check-ins and any
harvest in progress; it asks first and is refused twice within two minutes). The signal goes out
only after the response is sent, and only to a gunicorn master that is this process's own parent. Each box also has **history charts** (`box_history.py`): load, memory, root disk, network in/out and open
connections over the last 6 hours, 24 hours or 7 days, drawn as small inline-SVG charts with a hover readout and a
table of the latest readings. One sample per box per five minutes (`BoxSample`, kept seven days): an FTP box's comes
with its heartbeat, the HTTP box's from a small thread in each loader worker (all try, a unique index lets one win;
`LOADER_BOX_HISTORY=0` turns it off). A stretch with no readings is drawn as a gap, never as a line over nothing.
A **log viewer** sits on each box: pick a service (FTP box: proftpd, sync, relay, fail2ban, sshd;
HTTP box: loader, nginx) and 50/100/200/500 lines. It is not free text -- the request is a name
like `logs-proftpd-100` from those fixed lists, the box maps the word to its own journal unit and
runs `journalctl` itself (output up to 40 kB, newest lines kept); the HTTP box reads its own journal
now, an FTP box answers at its next check-in. There is no shell in the browser:
that stays on SSH (`box_commands.py`, `ftp-forwarder/ftp_forwarder/box_commands.py`).

**Automatic web bans (`auto_ban.py`).** Loader bans an address that keeps failing to
authenticate, like fail2ban does on the FTP box: **10 failures in 10 minutes -> an hour**,
**30 in 24 hours -> a day**. Counted: failed web logins and admin, FTP-box-sync and
bearer credentials (every refusal the Security box already records as a 401). A ban is a
temporary `AccessRule` block made by `auto-ban`, so the same middleware enforces it and it
reaches the FTP box's firewall too; the Security box lists it under *Banned now* ("loader
(web)") where an admin can Unban, move it to the blacklist (turning it into a permanent
entry in place) or whitelist it. It deliberately never bans: a **whitelisted** address (what
the whitelist is for), the **FTP box's own address** (a mis-set sync key must stay
recoverable), any **non-public** address (internal tools and health checks), IPv6 (nothing
else supports it yet), or an address already blocked -- and **push API key failures are not
counted**, because a provider's machine retrying with a stale key after a rotation would
otherwise be banned, and stay banned after fixing it. Safety valves: `LOADER_HTTP_AUTOBAN=off`
in the environment file turns it off, and the **"Clear automatic web bans" workflow** lifts
bans from the VM without the website (for an admin who got themselves banned) -- whitelist your
own office address in the Security box once you are in.

**Whitelist and blacklist (the Security box's "Address control").** One panel
holds three lists side by side -- *Banned now* (fail2ban, per FTP box),
*Blacklist* and *Whitelist* -- and every entry can be moved to another list
with a button (Banned -> Blacklist, Banned -> Whitelist, Blacklist <->
Whitelist, or removed), or added by hand with a note and an optional expiry
(`access_control.py`, `AccessRule`). **Blacklist**: loader answers every HTTP
request from it with 403 before anything else, even before the login redirect
(`access_middleware.py`, outermost; the refusal is recorded as *Blocked
address*), and the FTP box drops its packets (see `ftp-forwarder/README.md`).
**Whitelist**: never banned, and it *wins over* the blacklist, so a trusted
address inside a blocked range gets through; on the box it goes into fail2ban's
`ignoreip` and any current ban on it is lifted. IPv4 and IPv6 addresses and
ranges, nothing wider than a /8 (IPv4) or a /32 (IPv6); a source's allowed
senders take IPv6 too (proven against ProFTPD, FTP and SFTP). An
automatic web ban of an IPv6 address counts failures across, and bans, its whole /64. And the guards that matter: you cannot blacklist your
own address or the FTP box's (unless whitelisted first), since locking yourself or
the relay out cannot be undone from the page. How they combine with a push
source's own allowed senders: the blacklist is checked first and beats them (a
blacklisted provider address is refused even though the source lists it); the
whitelist never grants access to a source (an address not in a source's list is
still refused there) -- it only protects from bans and blocks. Because that
makes blacklisting a provider's address a way to cut them off silently,
blacklisting (adding, or moving from another list) an address or range that
overlaps any push source's allowed senders first shows a confirmation page
naming those sources (`access_control.affected_sources`; a sender wholly inside
a different whitelist entry is not counted, since that stays in). Each worker process caches the
rules for 5 seconds (at once on the one that made the change). The box learns the
rules from the reply to its heartbeat (`access`, with a `version`) and reports
the version it has applied, shown per box as *rules applied* / *waiting*.

`ui_admin_ftp.py`
puts an actual HTML page on this (`/admin/ftp`, in the masthead nav)
-- the account list plus live status for every remote FTP/SFTP-
receiving box (see `../ftp-forwarder/README.md`): since such a box is
deliberately pull-based/outbound-only, loader can't ask it "are you
alive" -- `FtpBoxStatus` just remembers the latest self-reported
heartbeat (`PUT /admin/ftp-box-status`, same narrow sync key as the
account-sync pull), and the page flags one as stale past 5 minutes
without a check-in.

**The FTP/SFTP receiving server itself is live and confirmed working**
on `MIN01RG96PVM02` -- ProFTPD, `mod_sql`-backed virtual users reading
directly from the same SQLite database the admin API writes to (no
separate sync step), serving both plain FTP (port 21) and SFTP via
`mod_sftp` (port 2222, its own `<VirtualHost>`) with one shared
account/credential set for either protocol. Real login + real file
upload confirmed on both. Two real, non-obvious fixes live in code
because of this: `models.FtpGroup` (an empty table ProFTPD's `mod_sql`
queries unconditionally during auth, even unused) and
`admin_ftp.hash_password`'s explicit `$2a$` bcrypt prefix (ProFTPD's
`mod_sql_passwd` here silently rejects bcrypt's own `$2b$` default).
See [`deploy/README.md`](deploy/README.md) for the full setup, both
gotchas explained, and what's still open (FTPS was explicitly dropped
in favor of SFTP, which needs no certificate; IP whitelist enforcement
is designed -- the data model and admin API both support it -- but not
enforced anywhere yet, deliberately paused after three real ProFTPD/
firewall approaches all turned out to be dead ends or to require
touching shared Azure networking beyond this one project's scope).

`providerharvest_service/ckan_tokens.py` -- the token-provisioning flow:
`get_or_provision_ckan_client(session, username)` returns a
`CKANActionClient` bound to that person's own personal CKAN API token,
minting one via the standing sysadmin credential (`CKAN_BASE_URL` +
`CKAN_SYSADMIN_API_TOKEN`) the first time they need one and storing it
envelope-encrypted (`CkanUserToken`, via the exact same
`engine/secrets/envelope.py` AES-256-GCM scheme already built for
provider credentials -- no second secret store invented). Confirmed via
tests, not just asserted: minting happens exactly once per user (a
second call reuses the stored token without minting again, checked
directly), the returned client actually carries the person's own token
(not the sysadmin's), and different users get different tokens.

`providerharvest_service/auth.py` -- the "who's making this request"
piece: `require_current_username` validates a Keycloak-issued JWT
bearer token (signature, issuer, audience, expiry) and returns the
caller's `preferred_username`. Built and fully tested against a
self-signed test keypair, independent of the real Keycloak client
actually being registered yet -- `KEYCLOAK_JWKS_URL`/`KEYCLOAK_ISSUER`/
`KEYCLOAK_AUDIENCE` are config values to fill in once that registration
happens (see the architecture discussion this design came out of), the
validation logic itself doesn't change. Key lookup is a narrow,
injectable `kid -> PEM` callable rather than a hard dependency on any
one JWKS-fetching library's own behavior, so tests exercise the real
signature-verification code path -- forged/expired/wrong-issuer/
wrong-audience/tampered tokens are rejected by the same logic that
runs in production, not a mocked-away approximation of it.

`GET /me` (in `app.py`) is the first endpoint to chain all of the
above together for real: Keycloak JWT -> `require_current_username` ->
`get_or_provision_ckan_client` -> a real CKAN Action API call
(`organization_list_for_user`) made AS that specific person, returning
which organizations they administer. That's the exact authorization
check the whole per-user-token design exists for -- CKAN's own
permission system answers it, not this service's own judgment.
Confirmed via tests: token minted on first call, reused (not re-minted)
on a second, and a request with no bearer token is rejected before any
CKAN call is even attempted.

`providerharvest_service/harvest_db.py` -- the SQLAlchemy engine/
session for this service's *harvested-data* store: a dedicated
PostgreSQL instance (`deploy/postgres/`), independent of `db.py`'s own
SQLite file (config only -- `ProviderSource`, `FtpAccount`,
`CkanUserToken` -- small and low-churn, staying on SQLite). Deliberately
lazy, unlike `db.py`'s eager module-level engine: `LOADER_HARVEST_DB_URL`
has no sensible default the way `db.py`'s SQLite path does, so requiring
it at import time would make importing this module a hard failure even
in the existing test suite (no Postgres available there at all). It only
becomes a hard requirement the first time `get_session()`/
`check_connectivity()` is actually called. `GET /admin/harvest-db/health`
(admin-key-gated) proves it end-to-end -- a real round-trip `SELECT 1`,
not just "the env var is set" -- deliberately its own endpoint rather
than folded into `/health`, since that one is deploy.yml's own smoke
test and coupling it to this new dependency would turn a Postgres blip
into a false-positive failed deploy for unrelated changes. Confirmed
the hard way that the connection string needs the explicit
`postgresql+psycopg://` scheme, not a bare `postgresql://` -- SQLAlchemy
defaults the bare scheme to the `psycopg2` dialect regardless of what's
actually installed, and this project installs `psycopg` (v3);
`/admin/harvest-db/health` 503'd with "No module named 'psycopg2'"
until that was fixed (`deploy/README.md`'s own provisioning workflow
now self-corrects an already-stored bare-scheme URL, rather than
requiring a one-off fix).

`providerharvest_service/harvest_models.py` -- the actual harvested-
data schema: `HarvestRun` (one execution attempt against a source --
`status` "running"/"success"/"error", `records_count`, `error_message`)
and `HarvestedRecord` (one fetched record's own parsed JSON `payload`,
tied to its run). A separate `DeclarativeBase`/metadata from `models.py`'s
own SQLite one, since these are genuinely different database engines --
`HarvestRun.source_id` references `ProviderSource.id` *logically*, not
via a real foreign key, and `source_name` is deliberately denormalized
(copied at run time) so a run's history still reads sensibly even if
its source is later renamed or deleted. A brand-new database with
brand-new tables needs no migration tooling yet -- plain `create_all()`
(called lazily, the first time `harvest_db.make_engine` actually runs)
is correct and sufficient; real migration tooling becomes worth it once
a schema change needs to land against tables that already hold data.

`providerharvest_service/harvest_runner.py` -- `run_harvest` actually
executes one: connects via the exact same `_build_transport` dispatch
`connection_check` already proved, fetches records, and stores them.
Manually triggered for this first slice ("Harvest now", a button on
`/provider/sources/{id}/runs`) -- an automatic scheduler that runs this
on each source's own `frequency` is the next slice, once this storage
pipeline itself is proven. Only `transport_type="http"` sources are
supported so far (SFTP/SCP/FTP are whole-file transports with nowhere
yet decided for the files themselves to land) and only `status="active"`
ones -- enforced in `run_harvest` itself, not just hidden in the UI, so
a direct POST can't bypass the approval workflow. A failed fetch is
recorded as `status="error"` with the real error message, never raised
past the function -- same "a failed check is exactly as valid a result
as a successful one" rule `connection_check` already uses.

`providerharvest_service/models.py`'s `ProviderSource` +
`providerharvest_service/provider_sources.py` -- this service's own
provider-source registration/approval lifecycle, independent of CKAN
(spec'd off `ckanext-providerharvest`'s `logic/action.py` and
`blueprints/provider_ui.py`, ported as *behavior* -- field shapes,
validation rules, the `pending -> active/rejected`,
`active <-> paused` state machine -- not as code; that plugin calls
`toolkit.get_action(...)` in-process, this service has no such thing
to call). Credentials (API key/basic/OAuth2/mTLS for HTTP, or
password/private-key for SFTP/SCP/FTP) are envelope-encrypted on the
row itself via the exact same AES-256-GCM scheme already used for
`CkanUserToken`, never stored in plaintext. **Deliberately does not
yet touch CKAN at all**: approving a source just flips its status --
no `harvest_source_create`/package/DataStore provisioning over the
Action API yet, that's a later slice once an actual harvest run needs
those to exist. Field-mapping rows (`row_rules`) and the
fetch-host-key dry-run endpoint aren't ported yet either --
registering a source today requires knowing (and typing in) an
SFTP/SCP host-key fingerprint gathered out-of-band, rather than
fetching it through this UI.

`providerharvest_service/connection_check.py` -- the first piece of
"external connection to the source" monitoring: `check_connection`
runs a real dry-run connect + one `list_entries` call against a
source's actual endpoint, using the already-built, already-tested
engine transports/auth strategies (`_build_transport` dispatches on
`transport_type` exactly like the old blueprint's
`provider_source_test_connection` did), and persists the result
(`last_check_status` "ok"/"error", a human-readable message, and the
timestamp) on the row regardless of outcome. Independent of the
approval `status` machine -- a still-pending source can be checked.
Fetches nothing beyond one page for the check itself; doesn't create
or store any harvested records/files -- that's the scheduled-harvest
slice this sets up for next (same `_build_transport` will be reused
there).

`providerharvest_service/db.py`'s `_sync_schema` -- there's no Alembic/
migration framework here, so `create_all()` alone isn't enough once a
model gains a column: it only ever creates tables that don't exist yet,
never alters one that's already there. Learned this the hard way in
production (adding `ProviderSource.last_check_*` 500'd every
source-listing page against the already-deployed SQLite file with "no
such column") -- `_sync_schema` now adds any column a model declares
but an existing table is missing, via SQLite's `ALTER TABLE ... ADD
COLUMN`, on every `make_engine()` call. Covered by a regression test
that reproduces the exact pre-migration table shape. Only ever adds a
column as nullable and raises clearly (rather than silently guessing,
or silently dropping the constraint) if a model ever declares a new
NOT NULL column against a table that already has rows -- that always
needs a deliberate migration (add nullable, backfill, tighten), not an
automatic one.

`providerharvest_service/proxy.py` -- the live API answers of providers
who don't want their own API publicly reachable at all (served at the
published `/data/...` address of an API source's pipeline): a GET forwards to exactly that source's registered
`endpoint_url` (the caller's query parameters passed through, auth
injected server-side via the same `engine/auth_strategies` every other
piece uses) and relays the response back -- one live call in, one live
call out, nothing scheduled, nothing stored. Only for
`transport_type="http"` sources that are `status="active"` -- a
pending/rejected/paused source's real endpoint and credentials are
never reachable through this route, not even to prove it exists (same
404 either way). Upstream response headers are never passed through
verbatim (only content/status/content-type) so an upstream `Set-Cookie`
or `WWW-Authenticate` can't leak to the caller.

Two independent, configurable rate limits, both `engine/transport/
rate_limit.py`'s `TokenBucket`, both in-process caches -- under this
service's real `gunicorn -w 4` deploy, each worker has its own
independent buckets, so the *effective* production ceiling for either
is up to ~4x its configured number, not exact (a cross-process limiter,
Redis or this service's own DB, is a follow-up once that imprecision
actually matters):
- `max_requests_per_minute` (config, default 60, also read by
  `connection_check`'s own `_build_transport`) -- this service's total
  outbound call rate to the provider, shared across every caller.
  Enforced with the blocking `acquire()` -- smoothing a burst by making
  a caller wait briefly is fine here.
- `proxy_rate_limit_per_ip` (config, default 30) -- how much of that
  shared budget any single calling IP may consume, keyed by
  `(source_id, caller_ip)` and bounded/LRU-evicted at 4096 tracked
  callers so this doesn't grow forever over a long-running process.
  Enforced with `TokenBucket`'s new non-blocking `try_acquire()`
  instead: an abusive caller gets an immediate 429 (with `Retry-After`)
  rather than tying up one of the 4 gunicorn workers sleeping on their
  behalf, which blocking would risk exhausting outright. Both fields
  are on the registration form's HTTP fieldset.

No consumer-level access control yet, matching this whole service's
current "no auth wall" posture -- anyone who knows a source's name can
call its proxy today.

Every other previously-hardcoded engine limit is now configurable too,
each on the registration form and stored in the same `config` JSON
blob, each with the exact default the engine used to bake in --
nothing about registering a source is tuned for values that don't
actually fit that provider:
- HTTP: `request_timeout_s` (30), `max_pages` (1000, the hard safety
  cap on pagination depth), `max_retries` (5, on a 429/5xx). The first
  two were already real `DirectHTTPSTransport` constructor params, just
  never read from config; `max_retries` wasn't even a constructor
  param at all before this -- it was `DEFAULT_MAX_RETRIES`, a bare
  module constant used directly inside the retry loop, so making it
  configurable meant a small, additive change to the transport itself
  (existing callers unaffected -- same default, opt-in override).
- SFTP/SCP and FTP: `connect_timeout_s` (30) each -- already
  constructor params on `SFTPTransport`/`ScpTransport`/`FTPTransport`,
  just never threaded through from config either.

Whether a source is proxied at all is **opt-out, not opt-in**:
`ProviderSource.proxy_enabled` defaults true (a checkbox on the
registration form, pre-checked) -- a provider who wants scheduled-only
harvesting and no live pass-through unchecks it. Nullable, deliberately:
`db._sync_schema` can only add new columns as nullable (see its own
entry above), so every source registered before this flag existed gets
SQL NULL for it -- treated as enabled everywhere this is checked
(`is False`, never plain falsiness), matching the behavior those
sources already had. The parsing itself defaults a *missing* form key
to disabled, same reasoning as `ftp_use_tls` -- an unchecked checkbox
sends nothing at all, indistinguishable from "never mentioned", so
only the form's own pre-checked default makes this opt-out in practice.

`providerharvest_service/build_info.py` -- real build metadata (git
commit + that commit's own timestamp, read straight from the deployed
clone's `.git`) shown in every page's footer, next to this project's
owning department and a link to the GitHub commit history -- so
"did my fix actually deploy" is answerable by looking at the page,
not digging through CI logs.

`providerharvest_service/ui_provider.py` + `ui_admin.py` -- the two
self-service/admin web UIs (server-rendered Jinja2 templates under
`providerharvest_service/templates/`, no JS build step -- a small
inline `<script>` just toggles which fieldset is visible for the
selected transport/auth type, matching the old blueprint's own
"works without JS, nicer with it" convention). `/provider/sources` +
`/provider/sources/new` are the provider-facing register/list/pause/
resume flow; `/admin/sources` is the admin dashboard: the approval
queue (only when something is waiting -- the number of waiting sources is
also shown on the dashboard's tab in the masthead, see `nav.py`) above the
"every source, any status" list. (`/admin/sources/pending`, the queue's old
page, redirects there.) Both link to the same shared `/provider/sources/{id}/edit` (any field,
any status, admin or provider -- one implementation, matching the
single-stand-in-user model) and a `/{id}/delete` with a confirm
prompt. Editing reuses the registration form itself, pre-filled from
`provider_sources.config_to_form_fields` -- credential fields are
always shown blank (never decrypted back to the browser); leaving one
blank keeps the existing encrypted secret, typing a new value rotates
it. That "leave blank to keep" behavior only holds when
transport_type/auth_type aren't changing -- switching either requires
the new credential's full shape, same validation as a fresh
registration (see `provider_sources.update`'s own docstring). Both now
show a **Health** column (reachable/unreachable/not checked,
last-checked timestamp) and a "Test connection" button per source,
wired to `connection_check.check_connection`. **No login wall on
either yet** -- every visitor acts as the single stand-in admin/
provider user the whole service currently runs as (see "not yet built"
below); that's a deliberate, temporary simplification to prove the
registration/approval workflow first, not an oversight. Visual design
matches data.gov.gr's own tokens (navy gradient masthead, Inter font,
Bootstrap-standard status colors -- confirmed against the live site's
own `data_gov_gr.css`), so this reads as part of the same platform.

`harvest_runner.py` also stores whole files now, not just HTTP records:
`sftp`/`scp`/`ftp` sources are whole-file transports (`FILE_TRANSPORT_
TYPES`), so instead of `HarvestedRecord` each fetched file becomes a
`HarvestedFile` row (filename, a content type from a small fixed
extension table -- deliberately not `mimetypes.guess_type`, which
consults the OS's own registered types and gave a different answer on
a Windows dev box than a Linux production VM would; size; the raw
bytes). Stored as a Postgres blob (`LargeBinary`) directly in the same
`loader_harvest` DB, not a filesystem path or object storage -- these
sources are documented to feed several small files (each fetch well
under 30s), so there's no real infra benefit yet to a separate storage
layer, matching every other storage choice this service has made so
far (own DB, prove the smallest slice first). Revisit if a source ever
actually needs large files. `open_entry`'s stream is read in capped
chunks (capped at `harvest_runner.MAX_FILE_SIZE_BYTES`, one system-wide
100 MB limit that matches nginx and the WAF -- not a per-source setting)
rather than a single `.read()` --
the file transports deliberately hand back unbuffered handles so a
huge/misconfigured file errors the run before it's fully buffered into
memory, not after. A file is downloaded via `GET /provider/sources/
{id}/runs/{run_id}/files/{file_id}`, served with its own content type
and a `Content-Disposition: attachment` header.

`providerharvest_service/scheduler.py` -- the automatic recurring
harvester: `run_due_harvests` runs `harvest_runner.run_harvest` on
every active source of a transport type `harvest_runner` supports
(`SUPPORTED_TRANSPORT_TYPES` -- http, sftp, scp, ftp) whose own
`frequency` (DAILY/WEEKLY/MONTHLY) has elapsed since its last run (or
that's never run, or whose last run has been stuck "running" for over
an hour -- a worker that crashed mid-fetch shouldn't permanently block
that source). `MANUAL` sources are never picked up here, by design.
`start_background_thread` runs
this on a periodic daemon thread (`LOADER_SCHEDULER_INTERVAL_S`,
default 300s), started from `app.py`'s own `lifespan` and stopped
cleanly on shutdown via a `threading.Event`, not just left to
`daemon=True`. Deployed as `gunicorn -w 4` (see deploy/loader-data-gov-gr.service),
so all four worker processes run their own copy of this loop --
coordinated with a Postgres session-level advisory lock
(`pg_try_advisory_lock`, held for the whole tick and released
explicitly, not the simpler but wrong-here transaction-scoped
`pg_try_advisory_xact_lock`, since `run_harvest` itself commits
partway through its own work) so only one worker's tick actually scans
for due sources and starts runs; skipped entirely against a non-
Postgres session (the SQLite stand-in tests use, with no such
concurrency to guard against). `LOADER_SCHEDULER_ENABLED=0` disables
it outright (e.g. for a one-off diagnostic process that shouldn't also
be harvesting). Proved end-to-end locally before shipping: register a
source, activate it, touch nothing else, and watch it get harvested by
itself on the next tick -- no "Harvest now" click.

`providerharvest_service/push_ingest.py` -- `transport_type="push"`,
the inverse of every other transport: the provider calls *this*
service instead of it connecting out. Exists specifically because real
FTP/SFTP-push receiving (a provider given this VM's own IP/credentials
to push to, via the ProFTPD server `deploy/README.md` already sets up
and confirms working end-to-end) needs a public network path into the
VM that doesn't exist yet -- this VM's NIC has no public IP, and
Application Gateway can't carry raw FTP/SFTP no matter how it's
configured (it's Layer-7 HTTP(S)-only). Attaching a public IP directly
to the VM's NIC would also be a real step backwards: NSG rules can't
tell which public IP a packet arrived through, so a second public IP
on the same NIC would make port 80/443 *also* reachable directly on
it, bypassing the Application Gateway's WAF entirely -- a Load
Balancer (whose rules only forward the specific ports configured, no
others) or Azure Storage's native SFTP support (a separate, fully
Azure-managed endpoint, no VM networking involved at all) are the two
options that don't have that problem, whenever real FTP/SFTP-push
support is actually built.

Meanwhile, `push_ingest.py` gets the same underlying capability
("provider pushes, we don't pull") shipped today with zero new
infrastructure -- it's just another route on the same app, reachable
over the exact same HTTPS path everything else already uses:
- Registering a push-mode source needs no endpoint/credentials at all
  (there's nothing to connect to) -- instead we generate an API key
  and show it to the provider exactly once, on its own confirmation
  page (`provider/push_credentials.html`), alongside the exact upload
  URL to give them.
- That key is stored as a bcrypt hash (`push_api_key_hash`, in the
  source's own `config` blob), not through the envelope-encryption
  path every other transport's *outbound* credentials use -- it only
  ever needs to be verified against what a caller presents, never
  decrypted and used by this service itself, so one-way hashing is
  strictly safer here. Same "generate once, never recoverable again"
  choice already made for FTP-push account passwords (`admin_ftp.py`).
- `POST /push/{source_name}/upload` (multipart, `X-Api-Key` header)
  checks that key, reads the file in capped chunks (same
  system-wide `MAX_FILE_SIZE_BYTES` and reasoning as the pull-mode
  file transports), and stores it via `harvest_runner.
  record_pushed_file` -- a normal `HarvestRun`+`HarvestedFile` pair,
  so run history/download UI works identically to a pulled file. Same
  404-regardless-of-reason posture as `proxy.py`'s lookup (unknown
  name, wrong transport type, and not-yet-active source all get the
  same response) -- a pending/rejected source's existence must not be
  discoverable via this public route.
- Never touched by `scheduler.py` -- there's nothing to poll; the
  provider's own upload calls are what create runs, on no schedule at
  all.

Not yet built, in the rough order they'll likely be needed:
- Real FTP/SFTP-push receiving for providers who can only push, not
  call an HTTPS endpoint -- the ProFTPD server itself is proven
  working end-to-end already (see `deploy/README.md`'s FTP-push
  section); what's missing is purely the public network path into
  this VM (see `push_ingest.py`'s own docstring above for why this
  isn't trivial), a decision outside this repo's own scope.
- Actually registering the Keycloak client (`providerharvest-api`) so
  `KEYCLOAK_JWKS_URL`/`ISSUER`/`AUDIENCE` are real values instead of
  unset config -- an Azure/Keycloak-console task, not code -- and then
  wiring the two UIs above to it (real per-user login, a real "which
  org can this person register a source for" check via
  `organization_list_for_user`, replacing the single stand-in user).
- Field-mapping profiles (`row_rules`) and the fetch-host-key dry-run
  endpoint the old blueprint had.
- Provisioning an approved source for real in CKAN (harvest-source
  package, DataStore resource) over the Action API via
  `CKANActionClient` -- deliberately deferred; harvested data lands in
  this service's own DB for now, not CKAN's DataStore.

## Running the tests

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows; drop the .exe/Scripts on Linux/macOS
./.venv/Scripts/python.exe -m pytest tests
```

No CKAN installation, no Docker, no database -- this is the whole point
of the split being checked for real, not just asserted. 309 tests,
all passing with zero CKAN present.

## Running the app locally

```bash
./.venv/Scripts/python.exe -m providerharvest_service.app   # or: uvicorn providerharvest_service.app:app --reload
curl http://127.0.0.1:8000/health
```

**The admin dashboard is part of the Extract Box.** It used to be a tab of its own that repeated the Extract Box's list and added two things: the approval queue (*Activate* / *Reject* with a reason for a newly registered source) and each source's data retention. Both now sit in the Extract Box and are shown only to a **super admin**: an *Awaiting approval* panel above the list, and *Activate* / *Reject* / *Data retention* in the actions. Everything under `/admin/sources/<id>/...` (approve, reject, pause, resume, delete, retention) is refused to anyone who is not a super admin (`admin_session.needs_superadmin`); `/admin/sources` itself, and the old `/admin/sources/pending`, lead to the Extract Box. The number of sources waiting for approval is on the Extract Box tab, for a super admin.

**Confirmations are a dialog inside the page.** Every "are you sure?" (delete a source, stop publishing, test a connection that makes a real call, replace a password...) and the reason asked when rejecting a source used to be the browser's own box. `base.html` now replaces `confirm()` and `prompt()` with one styled dialog: the templates still ask the usual way (`onsubmit="return confirm(...)"`), the first call opens the dialog and answers "no", and when the person says OK the same action is triggered once more and let through (a form is submitted again with the button that sent it). A destructive question gets a red button, Escape or a click outside cancels, Enter in a text box accepts, and the focus stays inside the dialog.

**A source belongs to a CKAN organization** (`ProviderSource.organization_id` / `organization_title`; the *Organization* picker on the source form's General tab). It decides who sees the source on the Overview: a super admin sees every source, anyone else the sources of the organizations they administer in CKAN. The picker lists every organization for a super admin (`organization_list`, through the sysadmin credential) and, for anyone else, the ones they administer (`organization_list_for_user` with permission `admin`, as them); the choice is checked against that same list on the server and the title is CKAN's. A person signed in with a CKAN account who is not a super admin must choose one; a super admin may leave it out (such a source is then seen by super admins only); a deployment without CKAN takes it as given. The Extract Box shows each source's organization, and "no organization" to super admins where it is missing.

**Overview** (`/overview`; `overview.py`, `ui_overview.py`, `templates/overview/flows.html`; the first tab). Every pipeline the signed-in person may see, drawn as a flow: the **input** (the source, with its type, status and health) -> the **transform** (the pipeline, on or off, its steps as chips; a request turned around is marked) -> the **output** (its format, how many outputs went well or failed, the latest) -> **where it goes** (the published address, and its CKAN resource or data service). The boxes are joined by curved lines; a line is *live* (an animated flow) when both of its ends are running, and a dashed grey one otherwise; every box is a link to the page that owns it (the source in the Extract Box, the pipeline in the Transform Box, the outputs, the Load Box, the CKAN page). A source with several pipelines fans out to each. They are grouped by CKAN organization, and filtered by a search box, by state (*Running*, *Needs attention* -- failed outputs, an unreachable source or a failed CKAN push --, *Paused or waiting*, *Not delivering*) and by organization. The lines are drawn in the browser between the real positions of the boxes, so they follow the layout when the window changes. **Who sees what:** a super admin sees every source; anyone else the sources of the organizations they administer in CKAN (`organization_list_for_user`, permission `admin`); a person without a CKAN account, or with CKAN out of reach, sees nothing and is told why.

The Overview is the **default screen**: `/` and a login that was not sent anywhere else land on `/overview`.

**Several loads per pipeline, and the DataStore** (`ckan_datastore.py`). The scheme is: one input has several transforms (pipelines), and each pipeline has several loads, each load exactly one way -- a **link**, an **uploaded copy**, **rows in a DataStore table** or a **data service** -- and each its own line in the Load Box (`CkanTarget` is one load; `/load/sources/<id>[/pipelines/<name>]/ckan?load=<id>` opens one, `?load=new` starts another) and its own CKAN box in the Overview. A DataStore load is written by the loader itself (`datastore_create` / `datastore_upsert`, in batches), not by uploading a file for xloader: the output (JSON, JSON Lines or CSV) is read as rows, the columns and their types are worked out from the values (a column keeps its type once the table exists), and each new output either **replaces** the table, is **added** to it (never the same output twice) or is **upserted** on key columns chosen on the page, so history can grow. Unpublishing a pipeline takes its links, copies and services out of CKAN but leaves a DataStore table alone, so its history is not lost.

**Keeping a limited history in a DataStore table, and creating the dataset** (`ckan_load.prune_target`, `ckan_datastore`). A DataStore load that adds rows or updates by key may be given a retention (the last 7, 14, 30, 90, 180 or 365 days, by the day of one chosen time column). Because CKAN can only delete rows that equal a value, not "older than", each row also carries its day in an extra column, `loader_day`; rows already older than the retention are not loaded at all (they would only be pruned again), and the loader's hourly cleanup (the same thread as the file retention above) prunes each table at most every six hours, two days at a time, oldest first. When chosen, each day is first saved as a CSV resource of the same dataset (in parts of 100,000 rows) and only then deleted from the table, so a failure never loses a day; what was done, or why it failed, is shown on the output's page. Several workers claim a pass with a compare-and-set on the saved setting, so only one does it. A new load can also **create the dataset** it goes into (the choice "A new dataset" on the output page): the details are those of a data service (licence, themes, legislation, high-value category, contact, publisher, keywords; remembered per organization), sent in the shapes a working dataset-creating script of the portal uses; the dataset is **private** until the box "make it public" is ticked or someone does it in CKAN, its address comes from the source name, and removing the load leaves the dataset in CKAN.

**The public address is an output, not a switch.** The Load Box no longer has Publish / Unpublish buttons or a "Published address" column: a pipeline's outputs are one column, each on its own line -- the **public address** (`/data/<source>/<pipeline>`, shown with its request settings for an API source and a *Remove* button) and each load into CKAN -- with "+ Add an output" (*Public address*, *Into CKAN*) beneath. Adding or removing the public address is what publishing and unpublishing were (`Transform.published` is still the setting behind it, and the same `/load/sources/<id>[/pipelines/<name>]/api` endpoint does it). A **link** or **data service** output points at that address, so saving one adds it if the pipeline has none (`publishing.ensure_published`; a source that is not active yet cannot, and the connection is kept with the reason); removing the address takes the links, uploaded copies and services out of CKAN, as unpublishing did, and leaves a DataStore table alone.

**Making an output's dataset public or private.** The page of an output that is in CKAN shows whether its dataset (for a data service, the entry itself) is **private** or **public** -- asked of CKAN each time the page is opened -- with a *Make public* / *Make private* button (`ckan_load.set_private`, a `package_patch` as the signed-in person, so CKAN's own permissions decide and a refusal is told in words). It changes the whole dataset, not only this output's resource, and making one private asks first.

A new dataset **and a new data service** are created with the visibility chosen on the form (*Who can see it in CKAN*: private, the default, or public); an entry that is already in CKAN keeps its own visibility, which is changed with the *Make public* / *Make private* button, and a later push never resets it (a data service used to be created public and re-sent as public with every update).

**API documentation of each public address** (`api_docs.py`). Every published pipeline has a Swagger page at `/api-docs/<source>[/<pipeline>]` and its OpenAPI 3.1 description at `.../openapi.json`, public like the address itself (exempt from the login) and a 404 for anything that is not published of an active source. It is made from what the loader knows: the path and method (for an API source, the one callers use, and the request template's parameters as required query parameters or a JSON body), the output format's content type with an example and, for a source that stores data, the column names and types and example rows of the newest output (a pass-through API source says "as the provider returns it" instead of inventing a shape), the ETag / 304 behaviour, the 404 / 429 / 502 answers and the per-address rate limit. The Swagger UI script and style are loaded from jsdelivr (`swagger-ui-dist`, a pinned version), so nothing is installed on the VM. The Load Box shows an *API docs* link on the public-address line, and a data service's `documentation` always includes the link.

**CSV files that are not a plain table** (`transform_engine.csv_options`; the reader step in the Transform Box, under "The file has lines before the table, no header row, or marks for 'no value'"). A CSV reader can skip a number of lines at the top, name the columns from a list when the file has no header row (typed, or filled from a header file such as EMY's `header_info.txt` in the browser: the first token of each line, a titles line skipped, a repeated name numbered), and turn values that match a pattern (`/+` for the `////` a weather station writes where it has no sensor) into empty ones. A date and time with no separators (`202604010150`, also 8 and 14 digits) is read as UTC by the *datetime* conversion. These are the pieces for the two-line files the EMY stations send.

**Chosen push-account passwords: the minimum is TEMPORARILY 6 characters** (it was 12). A provider's existing password has 7 characters and is to be changed in a few days; `ftp_credentials.MIN_CUSTOM_PASSWORD_LENGTH` reads `LOADER_MIN_PASSWORD_LENGTH` (never below 6, default 6 for now), so it goes back to 12 by setting that to 12 on the VM or by changing the default. The forms say the current number; generated passwords are unaffected. Limiting the account to the provider's own address (the account's allowed-IP list) is the sensible protection while it is short.
