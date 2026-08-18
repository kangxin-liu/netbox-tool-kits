# Implementation plan — NetBox bulk provisioning (iteration 1)

Dependency-ordered. Each task is small enough to review on its own and ends in a verifiable state.
Tests are written with the task they cover, not batched at the end. No task contacts a live NetBox
instance; HTTP is stubbed.

## Phase 1 — Scaffold and configuration

- [ ] 1. Create the Django project skeleton
  - `config/` project + `importer/` app, per `.kiro/steering/structure.md`
  - `requirements.txt` pinning Django 5.2 LTS, `pynetbox` 7.x, `openpyxl`, `python-dotenv`,
    `pytest`, `pytest-django`, `responses`, `ruff`; verify the Django patch release supports the
    local Python 3.14 before committing the pins
  - `ruff` and `pytest` config; confirm `pytest` collects and `ruff check .` passes on the empty tree
  - _Requirements: none (foundation)_

- [ ] 2. Environment-driven settings
  - Read `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`,
    `NETBOX_*`, `MAX_UPLOAD_MB`, `RACK_NAME_DELIMITER`, `NETBOX_DEFAULT_DEVICE_ROLE`,
    `NETBOX_AUTOCREATE_DEVICE_TYPES` from the environment
  - Fail fast at startup with the variable name when `NETBOX_URL`, `NETBOX_TOKEN`, or
    `DJANGO_SECRET_KEY` is missing; log a warning when SSL verification is disabled
  - Test: missing variable raises at import; no default secret key exists in source
  - _Requirements: 2.1, 2.2, 2.3, 14.1, 14.2_

## Phase 2 — Access control and shell

- [ ] 3. Local superuser-only authentication
  - Django auth login/logout views; no registration or password-reset routes
  - A default-deny base view mixin requiring authentication and `is_superuser`
  - Log login success, login failure, and logout with actor and timestamp
  - Tests: anonymous redirects to login; authenticated non-superuser gets 403; no seeded credentials
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.6, 14.7_

- [ ] 4. Base template and nav bar
  - `base.html` with the three-page nav, active-page marking, username, logout control
  - Header region showing the NetBox hostname and connection status — never the token
  - Placeholder views for hierarchy, devices, cabling at their final URLs
  - Tests: each page reachable and marks itself active; rendered HTML contains no token
  - _Requirements: 2.4, 3.1, 3.2, 3.4, 3.5_

## Phase 3 — The sheet contract

- [ ] 5. `columns.py` column definitions
  - `ColumnSpec` dataclass and the three column tuples exactly as tabulated in `design.md`
  - Synthetic example values only — no real site, device, or serial values
  - Test: every spec has help text; required columns match the requirements tables
  - _Requirements: 4.3, 4.6, 7 (columns), 8 (columns), 9 (columns)_

- [ ] 6. Template generation
  - Generate `.csv` and `.xlsx` templates from `ColumnSpec`, with a header row, a guidance row, and
    one synthetic example row; xlsx marks optional columns distinctly and adds picklists for
    enumerated fields
  - Wire the six download URLs
  - Test: a generated template parses cleanly through the parser from task 7 (round-trip)
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [ ] 7. Upload parsing
  - Extension and content-type allow-list, size cap before parse, `.xlsx` read-only with values not
    formulas, first-or-named worksheet, case-insensitive whitespace-trimmed header matching, any
    column order, blank-row skipping
  - Reject the file listing all missing required columns at once; warn on unknown columns; delete
    temp files in `finally`; never write to a served path
  - Refuse rows still holding the template's example values
  - Tests: one per criterion below
  - _Requirements: 5.1–5.10, 4.5_

## Phase 4 — NetBox access

- [ ] 8. NetBox client factory
  - Build `pynetbox` from settings; apply verify-SSL and timeout; probe `/api/status/` once per
    process and cache the version; classify failures as DNS / TLS / timeout / auth
  - Redact headers and token from every exception before it can surface
  - Tests (stubbed HTTP): each failure class maps correctly; version captured; token absent from
    exception text and log records
  - _Requirements: 2.5, 2.6, 12.7, 14.1, 14.5_

- [ ] 9. Lookup and choice cache
  - `get_or_none`, `prefetch`, `choices_for`, `terminations_for`, `occupied`, keyed per request
  - Bulk prefetch of sites, locations, racks, device types, roles, and device terminations so a
    2,000-row sheet issues a bounded number of requests
  - Tests: repeated lookups hit the cache once; choices come from the API not a hardcoded list;
    request count stays bounded as row count grows
  - _Requirements: 11.1, 15.1, 15.2_

- [ ] 9a. Target readiness check
  - Version probe enforcing the supported range `>=4.6, <4.7`, refusing to import outside it and
    naming both detected and supported versions
  - Startup validation of `NETBOX_URL`: require `https://` unless the host is loopback; refuse
    plaintext to any remote host; log and banner the loopback development case
  - Read-only readiness summary per importer: does the default device role resolve, how many device
    types exist, are there devices with front/rear ports — creating nothing
  - Tests: each out-of-range version refused; `http://` to a remote host refused; `http://localhost`
    accepted with a banner; readiness summary issues no writes; missing default role warns at page
    load
  - _Requirements: 2.3a, 2.3b, 2.7, 14.5, 17.2, 17.3, 17.4_

- [ ] 10. Rack naming module
  - `parse_rack_name` / `build_rack_name` with a configurable delimiter and exactly three segments
  - Tests: valid names; two and four segments rejected; empty segment rejected; alternate delimiter;
    case-insensitive comparison preserving supplied casing
  - _Requirements: 6.1, 6.2, 6.4, 6.5_

## Phase 5 — Validation framework and importer pipeline

- [ ] 11. Validation primitives in `validation.py`
  - `Severity`, `ValidationIssue` (file, sheet, line, column, value, expected, code, message,
    related line/object/url), `Verdict` with `errors` and `is_applyable`, and the issue-code registry
  - `RowError(code, column, value, expectation)` converting straight into a `ValidationIssue`
  - Parsed rows carry their spreadsheet line number so issues report the line Excel shows, header
    counted as line 1
  - Tests: `is_applyable` false with one error, false with unacknowledged warnings, false when
    expired, false when stages were not all reached, true only when genuinely clean; `location()`
    renders `file / sheet / line / column`
  - _Requirements: 10.5, 10.8, 10.9, 16.1, 16.2, 16.3, 16.4_

- [ ] 11a. Verify NetBox bulk-create atomicity (decision D7) — **spike, do before task 12**
  - Against a **lab** NetBox only, POST a list of objects to one endpoint where a single entry is
    invalid, then check whether the valid entries were created
  - Record the finding in `design.md`: if atomic, apply uses one bulk POST per endpoint and the
    half-complete window shrinks to the boundaries between endpoints; if not, apply stays row-by-row
    with ordered audit records
  - Do not run this against production; use a throwaway instance and clean up after
  - _Requirements: decision D7, 12.4_

- [ ] 12. Shared pipeline in `importers/base.py`
  - `RowOutcome`, `PlannedRow` (keyed by spreadsheet line), `CreateAction`, `Plan`, `RunReport`, and
    the `Importer` protocol split into `validate_local` (S1–S3), `validate_remote` (S4), `plan` (+S5)
  - `apply` refuses unless `plan.verdict.is_applyable`, re-runs S4 first and aborts with zero writes
    if anything moved, then walks actions in order recording a sequence number
  - Abort on auth failure or connection loss; strategy for a mid-run NetBox 400 follows task 11a
  - Tests: a plan with one error issues zero HTTP writes; stale S4 aborts before the first write;
    auth failure aborts; applied-before-abort rows reported in order
  - _Requirements: 10.1, 10.2, 10.4, 10.6, 10.7, 10.14, 11.1, 11.2, 12.3, 12.4_

- [ ] 13. Hierarchy importer
  - Create region → site → location → rack in that order, deriving slugs for regions and sites
  - Validate `rack_u_height`, defaults for `rack_width` (19) and `rack_status` (`active`),
    rack-name-versus-columns agreement, in-sheet duplicate racks, same datahall name under
    different datacenters treated as distinct
  - Split the checks across `validate_local` (S2/S3) and `validate_remote` (S4: slug collisions,
    existing objects matching or differing)
  - Tests: one per criterion in Requirement 7, including slug collision with a different object
  - _Requirements: 7.1–7.9, 6.3, 11.4, 11.5_

- [ ] 14. Device importer
  - Resolve rack, validate face, position and `bottom_ru + ru_height - 1` against rack height,
    detect overlap with existing devices and with earlier rows on the same face
  - Device-type resolution with `ru_height` cross-check; autocreate behind the env flag; role
    fallback to the configured default and rejection when neither resolves; optional serial,
    asset tag, tenant, status
  - Skip existing device names; reject in-sheet duplicates; preserve sheet order
  - RU-overlap detection must cover both existing devices (S4) and other rows in the file (S3), and
    name both spreadsheet lines when two rows collide
  - Tests: one per criterion in Requirement 8
  - _Requirements: 8.1–8.12, 16.6_

- [ ] 15. Cabling importer
  - Resolve each end by kind (`device` → interface, `panel` + side → front/rear port), reject
    ambiguous or missing ends and ports
  - POST to `/api/dcim/cables/` with `a_terminations` / `b_terminations` arrays; never touch the
    read-only cable-terminations endpoint
  - Validate `cable_type` and `length_unit` against API choices; default status `connected`
  - Skip rows whose termination is already cabled, naming the existing cable; reject same-port-both-
    ends and duplicate terminations across rows
  - No transceiver handling: the column is deferred to iteration 2 (D3), so a `transceiver` column in
    an uploaded file is reported as an unrecognised column and ignored
  - Tests: one per criterion in Requirement 9, asserting the exact payload shape; plus an assertion
    that the importer issues no `PATCH`, `PUT`, or `DELETE` request at all
  - _Requirements: 9.1–9.12, 11.3_

## Phase 6 — Operator flow

- [ ] 16. Validate endpoint and issue report UI
  - `/<page>/validate/` accepts the upload, runs S1–S5, stores the plan and verdict server-side
    scoped to the user, and writes nothing to NetBox
  - `_verdict_banner.html`: blocked with the error count, or ready with create/skip/warning counts
  - `_issue_list.html`: issues grouped by severity then line, each rendering file / sheet / line /
    column, received value, expectation, and message; both lines shown for duplicates; NetBox links
    for issues about existing objects; a per-code count so one systemic mistake reads as one problem
  - Display cap (default 500) with a truncation notice; `/runs/<id>/issues.csv` carries the full set,
    formula-escaped
  - Tests: validate issues zero writes; stages-not-reached is stated; localisation fields all
    present; truncation notice appears at the cap while the CSV stays complete
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.10, 16.1–16.11_

- [ ] 17. The apply gate
  - Apply is POST-only and CSRF-protected, and its **first** action is checking
    `verdict.is_applyable`, returning 409 with the reason otherwise — the disabled button is not the
    control
  - No partial-apply path exists in any request shape; a blocked file omits the apply button entirely
  - Warning acknowledgement endpoint; fingerprint binding; 30-minute expiry; refuse a second apply
  - Pre-apply S4 re-run aborting with zero writes when NetBox has moved, reporting what changed
  - Busy state to prevent double submission; plan preserved across navigation, cleared on logout
  - Tests: forged POST on a blocked plan returns 409 with zero HTTP writes; unacknowledged warnings
    are not applyable; changed fingerprint and expired verdict both refused; stale S4 aborts before
    the first write; re-apply refused; logout clears the plan
  - _Requirements: 10.6–10.9, 10.11–10.17, 3.3, 1.5, 14.3, 15.4_

- [ ] 18. Audit log and reporting
  - `ImportRun`, `ImportRowResult`, and `ValidationIssueRecord` models and migration, with no token,
    file, or credential field
  - Persist a run for **blocked attempts too**, with its verdict, stage list, counts, fingerprint, and
    issues, so a refused import is auditable
  - Persist per-row created object type, id, name, and apply sequence
  - Read-only `/runs/` history newest-first and `/runs/<id>/` detail; no edit or delete in the UI
  - Result and issue CSV downloads, escaping leading `=`, `+`, `-`, `@` against formula injection
  - Per-row NetBox links, NetBox field errors mapped to line and column, and an explicit statement
    that applied creates cannot be rolled back
  - Tests: blocked run persisted with its issues; counts correct; token absent from every persisted
    field; formula escaping; run records not mutable via any exposed view
  - _Requirements: 12.1, 12.2, 12.5, 12.6, 13.1–13.5_

## Phase 7 — Hardening and handover

- [ ] 19. Row-count bound and progress
  - Enforce a documented maximum row count per apply as an S5 check, telling the operator to split
    larger files rather than timing out mid-run; show progress or a busy state while applying
  - Test: a sheet over the limit is blocked with the limit stated and nothing created
  - _Requirements: 15.3, 15.4_

- [ ] 20. Gate integration test — the one that matters most
  - End-to-end per importer against a stubbed NetBox: upload a file with one bad row, assert the
    response is blocked, the issue names the right file/sheet/line/column, and **zero write requests
    were issued**; then fix the row, re-upload, and assert the apply succeeds
  - Repeat for a file that is clean at validation but whose stubbed NetBox state changes before
    apply: assert the abort happens with zero writes
  - _Requirements: 10.1, 10.6, 10.7, 10.14, 16.1_

- [ ] 21. Security regression suite and deploy check
  - Assert the token appears in no rendered page, log record, exception, or audit row, including on
    error paths; autoescaping verified with a cell containing markup
  - `python manage.py check --deploy` clean with `DEBUG=false`; confirm `.env` is git-ignored and no
    secret is present in tracked files or commit messages
  - Confirm every test fixture is synthetic
  - _Requirements: 14.1–14.7, 5.7, 12.7_

- [ ] 22. README and operator runbook
  - Setup, environment variables, creating the admin user, running the server
  - The rack naming convention, the three sheet formats, and where to get templates
  - How to read a validation report and fix a blocked file; that apply is impossible until validation
    is clean; what to do if a run still aborts partway; that iteration 1 never updates or deletes
  - **Empty-instance prep runbook:** local NetBox via `netbox-docker` (Postgres and Redis included),
    creating the device roles in use, importing device types from `netbox-community/devicetype-library`,
    and building one patch panel with front-to-rear mappings in the 4.5+ `rear_ports` format — plus
    the note that sheet 1 works on an empty instance while sheets 2 and 3 need this prep first
  - _Requirements: 12.5, 6.1, 16.1, 17.6, 17.7_

## Deferred to iteration 2 (do not build now)

Update and delete of existing objects; IPAM and VLANs; multi-user roles and SSO; background workers
for very large sheets; multi-instance NetBox switching; compensating rollback of applied creates.

**Transceiver recording (D3, D6).** Dropped from iteration 1 because setting `interface.type` cannot
hold a part number once device types define interface types. Iteration 2 should pick between a
`dcim.InventoryItem` bound to the interface and a module in a module bay, then build the part-number
map from the transceiver models actually in use. Note this reintroduces writes to objects the tool did
not create, so the create-only invariant and its test will need revisiting deliberately.
