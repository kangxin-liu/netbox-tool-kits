# netbox-tool-kits

An internal Django web app for populating NetBox from spreadsheets. Iteration 1 **creates** DCIM
objects only — it never updates or deletes anything in NetBox, except the device importer's
field-scoped PATCH of a new device's primary IP after the matching address is created.

Three importer pages, one per nav-bar entry:

| Page | Creates | Sheet key |
| --- | --- | --- |
| Hierarchy | Location (region), datacenter (site), datahall (location), racks | one row per rack |
| Devices | Devices mounted in racks by face and rack unit, plus loopback and management addresses | `rack` name |
| Cabling | Cables between devices and patch panels | end names + port names |

Rack names follow `<datacenter_slug>-<datahall_slug>-<rack>`, e.g. `SYD1-DH1-R01`. That convention
is the join key between all three sheets. The first two segments are the datacenter's and datahall's
**slugs**, which the hierarchy sheet supplies in its optional `datacenter_slug` and `datahall_slug`
columns — so a site can be called "Sydney One" and still sit in a rack named `SYD1-DH1-R01`. Leave a
slug cell blank and it is derived from the name, which is all a single-token name like `SYD1` needs.

Two runbooks, for the two people who will read this:

- [`docs/operator-runbook.md`](docs/operator-runbook.md) — filling a sheet in, reading a verdict,
  fixing a blocked file, and what to do when a run aborts partway.
- [`docs/empty-instance-prep.md`](docs/empty-instance-prep.md) — preparing a NetBox that has never
  been used: a local instance, device roles, device types, and a patch panel with real port mappings.

## The validation gate

Every upload is validated in full before anything is written, and **one blocking error makes the
whole file unappliable** — there is no partial apply, because a half-populated NetBox is the failure
this tool exists to prevent. Each problem is reported with its exact location:

```
ERROR  RU_RANGE_OVERLAP
  file devices-syd1.xlsx, sheet Devices, line 47, column bottom_ru
  received "12", expected a range not overlapping rack SYD1-DH1-R01 front face
  syd1-dh1-r01-leaf01 would occupy U12-U13, already taken by line 31 (syd1-dh1-r01-leaf02)
```

Validation runs in five stages — file structure, cell format, consistency within the file,
cross-reference against NetBox, and feasibility — and the NetBox stage is re-run immediately before
the first write, aborting with nothing written if anything changed in the meantime.

## Status

**Complete for iteration 1.** All seven phases of `.kiro/specs/netbox-bulk-provisioning/tasks.md` are
built:

- **Foundation (1–7).** The Django project and its fail-fast environment configuration, superuser-only
  access with audited logins, the three-page nav shell, the shared column contract, template downloads
  in both formats, and upload parsing.
- **NetBox access (8–10).** A client that classifies every failure into something actionable, redacts
  the token from anything it raises or logs, and refuses at the transport layer to issue any method
  other than GET, OPTIONS, or POST. A caching lookup layer that keeps a 2,000-row sheet to a handful
  of requests, a read-only readiness panel per page, and the rack naming convention in one module.
- **Validation and the three importers (11–15).** The five-stage pipeline and the verdict that gates
  apply, plus the hierarchy, device, and cabling importers. All three validate and plan; apply issues
  one bulk POST per endpoint, and refuses outright unless the verdict is clean.
- **The operator flow (16–18).** Upload, verdict, issue report, plan preview, and apply, all in the
  browser. The validated plan is held server-side and applied only through the gate: a blocked file
  renders no apply button and a direct POST is answered with 409 and the reason. Every attempt —
  including a refused one — is recorded in a read-only run history with a per-row report and CSV
  downloads.
- **Sheet-supplied slugs (18a).** The hierarchy sheet's optional `datacenter_slug` and `datahall_slug`
  columns, and rack names built from those slugs rather than from the two display names, so a site can
  be called "Sydney One" without lengthening every rack name under it. A blank slug cell still derives
  one from the name, which is why sheets written before this change keep validating unchanged.

- **Hardening and handover (19–22).** The row-count bound stated on every page and enforced as a
  validation error, so a file too large to finish in one request is refused rather than timed out; an
  end-to-end gate test per importer that fixes a broken file, applies it, and proves the refused
  attempts wrote nothing; a security regression sweep over every page, log record, audit row, and
  error path; and the two runbooks above.

The suite is 757 tests and runs in about twenty seconds, with no live NetBox and no sockets.

The design lives in `.kiro/`:

```
.kiro/
├── steering/                 # persistent project context for the agent
│   ├── product.md            # what this is for, and what is out of scope
│   ├── tech.md               # stack, security baseline, NetBox API conventions
│   └── structure.md          # file layout and organising principles
└── specs/netbox-bulk-provisioning/
    ├── requirements.md       # 17 requirements in EARS notation + decisions D1–D8
    ├── design.md             # architecture, validation gate, sheet columns, threat model
    └── tasks.md              # 24 dependency-ordered implementation tasks
```

Read `requirements.md` first — Requirement 10 (the validation gate) and Requirement 16 (issue
localisation) are the heart of it. Every design decision is now settled. D7 was measured rather than
assumed: bulk creates are atomic on NetBox 4.6.8, so apply issues one bulk POST per endpoint. Re-run
`scripts/verify_bulk_atomicity.py` after a NetBox upgrade to confirm that still holds. D8 splits an
"exists but differs" conflict by field — blocking when the difference invalidates later rows,
warning when it cannot. Transceiver recording (D3, D6) is deferred to iteration 2.

Field names in the spec were checked against the target instance's own `OPTIONS` responses on NetBox
4.6.8 before any importer was written — see "Verified against the target instance" in `design.md`.
That caught the hierarchy sheet aiming its form-factor column at `Rack.type`, a field NetBox renamed
in 4.2, where `rack_type` now belongs to a foreign key pointing at a different model.

Every write in iteration 1 is the creation of a new object. There is no code path that updates or
deletes anything, and the test suite asserts no `PATCH`, `PUT`, or `DELETE` is ever issued.

## Getting started

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in your NetBox URL and token, and generate a secret key
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver          # http://127.0.0.1:8080/
```

`runserver` serves **8080**, not Django's usual 8000, because 8000 is where a local NetBox listens.
Pass an address to override it: `python manage.py runserver 9000`.

Authentication is a single local Django superuser. There is no registration flow and no SSO.

The app refuses to start when `NETBOX_URL`, `NETBOX_TOKEN`, or `DJANGO_SECRET_KEY` is missing, and
names the variable rather than falling back to a default nobody chose. It also refuses an `http://`
NetBox URL unless the host is loopback, so the plaintext exception cannot be pointed at a shared
instance.

## Configuration

All configuration comes from the environment — see `.env.example` for the full list. Required:
`NETBOX_URL`, `NETBOX_TOKEN`, `DJANGO_SECRET_KEY`.

`NETBOX_REVERT_TOKEN` is optional and defaults to `NETBOX_TOKEN`. A shared deployment should supply a
separate token with read/delete and only the field-scoped PATCH permissions required for reversion,
but no create permission.

## Secrets

`.env` is git-ignored and must stay that way. Never put a real token in source, a template, a log,
a commit message, or a spec file. If a token has been shared or pasted anywhere, rotate it in
NetBox. Tokens should be scoped to the forward or revert permissions they actually need.

## Development

```bash
pytest
ruff check . && ruff format --check .
python manage.py check --deploy   # must be clean before anyone else uses it
```

Tests never contact a live NetBox instance, and an autouse fixture fails any test that tries to open
a socket. HTTP is stubbed with `responses` for the client and lookup layers; importer tests run against
`FakeNetBox`, an in-memory instance that reproduces the API behaviours which change outcomes — ignored
query parameters, atomic list POSTs, and injectable failures.

The view tests drive the real forms through Django's test client against `FakeNetBox`, so a request
that should never write is checked by asserting the fake received no POST at all. Three files are
worth reading first:

- `test_apply_gate.py` tries a forged POST on a blocked plan, a stale fingerprint, an expired verdict,
  unacknowledged warnings, a second apply, and a NetBox that moved after validation. Every one ends
  with zero writes.
- `test_gate_integration.py` runs the whole flow once per importer: a file with one bad row is refused
  having written nothing, the row is fixed, the file applies, and then a NetBox that changes underneath
  a clean plan aborts it before the first write.
- `test_security.py` is the sweep that catches a *new* surface leaking something — a sentinel token
  checked against every page, log record, audit row, download, and error path, plus autoescaping,
  `check --deploy`, and the assertion that no tracked file carries a secret.

Fixtures are synthetic only: no production site names, serials, or customer identifiers.
