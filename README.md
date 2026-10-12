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
| Admin dashboard, **Data boxes**, Security box | operating it: approvals and retention; the HTTP and FTP machines; access control | built |

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
`/proxy/*`, the live open-data proxy, which is public by design. A route
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
source in the portal and on the admin dashboard; one home for a push source's
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

**Login** (`/admin/login`): the site sits behind a session; the session is started either by the shared username/password (the original placeholder) or, once configured, by the data.gov.gr Keycloak (`oidc_login.py`: authorization code + PKCE, signature/issuer/audience/nonce checks, role `loader-admin` required) -- setup in `deploy/README.md`; with `LOADER_OIDC_ACCESS=ckan-org-admin` the rule is CKAN's (a sysadmin or an administrator of an organization, asked of CKAN with the loader's sysadmin key). The person's own Keycloak user name then appears everywhere the shared name did.

**Integrity of what arrives** (FTP/SFTP push): the box's catch-up sweep skips a file that is still being written; the box sends a SHA-256 and the loader refuses a mismatch and answers with the hash of what it stored (a transfer counts as done only when both agree); a provider may also send `<file>.sha256` before each file for an end-to-end check, and a file that does not match it is put aside on the box instead of sent; each run's detail page shows the stored file's SHA-256. Details and a ready-to-send provider note: `ftp-forwarder/README.md`.

**Load Box** (`/load`; `load_api.py`, `ui_load.py`, `LoadTarget`). Per source, an admin can *publish* the transformed output: `GET /data/{source name}` (also `/data/{source name}/{any file name}`, for tools that want an extension) then answers, to anyone and without a login, with the newest successfully transformed output that still has its contents, in its own content type, with an `ETag` (conditional requests get 304) and a 60 s cache hint. Off by default; only for an active source; an unknown, unpublished, paused or empty source is the same 404; per-address limit of 60/min (shared limiter with `/proxy`). The planned next destination is a CKAN resource that links to this address, pushed automatically after each transform and by hand.

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

**Data retention per source** (`retention.py`; Admin dashboard -> a source's "..." menu -> *Data retention*). Two
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

`providerharvest_service/proxy.py` -- a live API proxy for providers
who don't want their own API publicly reachable at all: `GET
/proxy/{source_name}` forwards to exactly that source's registered
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
