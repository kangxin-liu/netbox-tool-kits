# netbox-tool-kits

An internal Django web app for populating NetBox from spreadsheets. Iteration 1 **creates** DCIM
objects only — it never updates or deletes anything in NetBox.

Three importer pages, one per nav-bar entry:

| Page | Creates | Sheet key |
| --- | --- | --- |
| Hierarchy | Location (region), datacenter (site), datahall (location), racks | one row per rack |
| Devices | Devices mounted in racks by face and rack unit | `rack` name |
| Cabling | Cables between devices and patch panels | end names + port names |

Rack names follow `<datacenter>-<datahall>-<rack>`, e.g. `SYD1-DH1-R01`. That convention is the
join key between all three sheets.

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

**Spec only — no application code yet.** The design lives in `.kiro/` and is meant to be reviewed
before implementation starts:

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
localisation) are the heart of it. Its **Still needs input** section lists what remains open: whether
NetBox bulk creates are atomic (D7, needs a lab experiment) and how strict an "exists but differs"
conflict should be (D8). Transceiver recording (D3, D6) is deferred to iteration 2.

Every write in iteration 1 is the creation of a new object. There is no code path that updates or
deletes anything, and the test suite asserts no `PATCH`, `PUT`, or `DELETE` is ever issued.

## Getting started (once Phase 1 of `tasks.md` is built)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in your NetBox URL and token
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Authentication is a single local Django superuser. There is no registration flow and no SSO.

## Configuration

All configuration comes from the environment — see `.env.example` for the full list. Required:
`NETBOX_URL`, `NETBOX_TOKEN`, `DJANGO_SECRET_KEY`.

## Secrets

`.env` is git-ignored and must stay that way. Never put a real token in source, a template, a log,
a commit message, or a spec file. If a token has been shared or pasted anywhere, rotate it in
NetBox. The token this app uses should be scoped to the DCIM write permissions it actually needs.

## Development

```bash
pytest
ruff check . && ruff format --check .
python manage.py check --deploy   # must be clean before anyone else uses it
```

Tests never contact a live NetBox instance — HTTP is stubbed. Fixtures are synthetic only: no
production site names, serials, or customer identifiers.
