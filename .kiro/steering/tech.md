# Technology stack

## Runtime

- **Python** 3.12–3.14 (developed on 3.14).
- **Django** 5.2 LTS — server-rendered templates, no SPA, no JS build step.
 Verify the pinned patch release supports the local Python before committing `requirements.txt`.
- **SQLite** for local app state (users, import run audit log). The app stores no NetBox
 mirror; NetBox is the system of record.

## Key dependencies

| Package | Purpose | Notes |
| --- | --- | --- |
| `django` | web framework | 5.2 LTS line |
| `pynetbox` | NetBox REST client | official NetBox Labs client; 7.x |
| `openpyxl` | read/write `.xlsx` | templates and uploads |
| `python-dotenv` | load `.env` in local dev | never used to load real secrets in prod |
| `pytest` + `pytest-django` | tests | |
| `responses` or `requests-mock` | stub NetBox HTTP in tests | no live NetBox in CI |
| `ruff` | lint + format | |

CSV is handled with the standard library `csv` module. **Do not add pandas** — it is a large
dependency for parsing sheets of a few thousand rows.

Before adding any dependency, confirm the name is not a typosquat, prefer packages already in
`requirements.txt`, and avoid known-vulnerable versions.

## Configuration

All configuration comes from environment variables, loaded via `.env` in local development
(see `.env.example`). Required: `NETBOX_URL`, `NETBOX_TOKEN`, `DJANGO_SECRET_KEY`.

- **No secrets in source, templates, logs, or commit messages.** Ever.
- The NetBox token is read from the environment at request time and passed to `pynetbox`.
 It is never rendered in a template, echoed in an error message, written to the audit log,
 or included in a traceback shown to the user.
- `NETBOX_VERIFY_SSL` defaults to `true`. Setting it to `false` is a lab-only escape hatch and
 must log a warning at startup.

## Security baseline

- Every view except the login page requires an authenticated superuser.
- `DJANGO_DEBUG=false` outside local development; `ALLOWED_HOSTS` always set explicitly.
- Uploads: extension and MIME allow-list (`.csv`, `.xlsx`), size cap (`MAX_UPLOAD_MB`), parsed
 in memory or from a temp file that is deleted in a `finally` block. Never persisted to a
 web-served directory, never `eval`'d, never trusted for filenames.
- Treat uploaded cell content and NetBox API responses as **untrusted input**: validate types,
 always autoescape in templates, never build HTML from cell values.
- Use `django.utils.crypto.get_random_string` / `secrets` for any randomness. No custom crypto.
- HTTPS to NetBox, certificate verification on. The **only** permitted `http://` target is a loopback
 host (`localhost`, `127.0.0.1`, `[::1]`) for local development, which logs a warning and shows a
 development banner. Plaintext to any remote host is refused at startup — the exception is scoped by
 host, not by a boolean anyone can flip.

## NetBox API conventions

- Target **NetBox 4.6.x** (latest at time of writing: 4.6.8). Support `>=4.6, <4.7` and refuse to
 run against anything outside that range with a clear message, rather than failing mysteriously
 mid-import. Read `/api/status/` on startup to record the version in the audit log.
- **NetBox 4.5 was a breaking change for patch panels.** `FrontPort.rear_port` and
 `rear_port_position` were removed in favour of a `PortMapping` model, exposed on the API as a
 `rear_ports` array of `{position, rear_port, rear_port_position}`; `RearPort` gained `front_ports`.
 The cabling importer only *reads* existing front and rear ports by name, so it is unaffected — but
 anything that creates panels or panel device types is version-specific, and the pre-4.5 field names
 must not be used.
- **Cables**: always POST to `/api/dcim/cables/` with `a_terminations` / `b_terminations` arrays
 of `{"object_type": "dcim.interface", "object_id": <id>}`. The
 `/api/dcim/cable-terminations/` endpoint is read-only in current NetBox and must not be used.
- Choice fields (`cable.type`, `rack.width`, `device.face`, statuses) are validated against
 values fetched from the API rather than hardcoded lists, and cached per process.
- Look objects up by natural key (`name` + parent scope, or `slug`) to keep imports idempotent.
- NetBox has **no cross-request transaction**. A partially applied batch is possible, so every
 run records what it created, in order, for manual unwind. Do not fake atomicity.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in locally
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
pytest
ruff check . && ruff format --check .
```

## Code conventions

- Type hints on all function signatures; `from __future__ import annotations` not needed on 3.12+.
- Business logic lives in plain functions/classes in `services/` and `importers/`, isolated from
 Django views so it can be unit-tested without HTTP.
- Views stay thin: parse request, call a service, render a result.
- Comments explain constraints and intent, not what the next line does.
- Mark substantially AI-generated modules with a short header comment
 (`# AI-assisted (Cursor) — review before merge`) for traceability at review time.
