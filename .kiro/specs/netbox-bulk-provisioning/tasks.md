# Implementation plan — NetBox bulk provisioning (iteration 1)

Dependency-ordered. Each task is small enough to review on its own and ends in a verifiable state.
Tests are written with the task they cover, not batched at the end. No task contacts a live NetBox
instance; HTTP is stubbed.

## Phase 1 — Scaffold and configuration

- [x] 1. Create the Django project skeleton — **done**
  - `config/` project + `importer/` app, per `.kiro/steering/structure.md`
  - `requirements.txt` pins Django 5.2.17 (the 5.2 LTS line declares Python 3.14 support from
    5.2.7; verified importing under the local 3.14.3), `pynetbox` 7.8.0, `openpyxl` 3.1.5,
    `python-dotenv` 1.1.1, `pytest` 8.4.2, `pytest-django` 4.11.1, `responses` 0.26.2, `ruff` 0.14.4
  - `ruff` and `pytest` config live in `pyproject.toml`; `ruff check .` and `ruff format --check .`
    are clean across the tree, including the pre-existing `scripts/`
  - _Requirements: none (foundation)_

- [x] 2. Environment-driven settings — **done**
  - Read `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`,
    `NETBOX_*`, `MAX_UPLOAD_MB`, `RACK_NAME_DELIMITER`, `NETBOX_DEFAULT_DEVICE_ROLE`,
    `NETBOX_AUTOCREATE_DEVICE_TYPES` from the environment
  - Fail fast at startup with the variable name when `NETBOX_URL`, `NETBOX_TOKEN`, or
    `DJANGO_SECRET_KEY` is missing; log a warning when SSL verification is disabled
  - Test: missing variable raises at import; no default secret key exists in source
  - Also brought forward from task 9a, because leaving plaintext-to-remote accepted in the interim
    would be a live hole: `config/env.py` validates the `NETBOX_URL` scheme at startup, permitting
    `http://` only to a loopback host and refusing it to any remote one
  - The suite runs on `config.settings_test`, which supplies synthetic values and then imports the
    real settings unchanged — pytest-django reads settings before a root `conftest.py` would load,
    and weakening the fail-fast rule to make tests run would have defeated the point of task 2
  - _Requirements: 2.1, 2.2, 2.3, 2.3a, 2.3b, 14.1, 14.2, 14.5_

## Phase 2 — Access control and shell

- [x] 3. Local superuser-only authentication — **done**
  - `LoginView` / `LogoutView` wired individually rather than including `auth.urls`, which would
    have added the password-reset routes Requirement 1.4 excludes
  - `SuperuserRequiredMixin` in `importer/access.py`: anonymous redirects, non-superuser gets 403
  - Login success, login failure, and logout logged to `importer.audit` with the actor; the failure
    receiver takes only the username from the submitted credentials, never the password
  - Tests: anonymous redirects to login; authenticated non-superuser gets 403; no seeded
    credentials; no password-reset/registration route reverses; logout clears the session
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 14.7_

- [x] 4. Base template and nav bar — **done**
  - `base.html` with the three-page nav, active-page marking, username, logout control
  - Target region shows scheme and host from a context processor; the token is not in the template
    context at all, so no template can render it. Live connection status arrives with task 8
  - Loopback-development and unverified-TLS banners
  - `static/css/app.css` is hand-rolled rather than Bootstrap 5, agreed on review and now recorded
    in `design.md`: a framework is a supply-chain dependency to review and patch, and these pages
    need layout rather than behaviour
  - Tests: each page reachable and marks itself active; rendered HTML contains no token; both
    banners render; the login page reveals no target
  - _Requirements: 2.4, 3.1, 3.2, 3.4, 3.5_

## Phase 3 — The sheet contract

- [x] 5. `columns.py` column definitions — **done**
  - `ColumnSpec` dataclass and the three column tuples exactly as tabulated in `design.md`. The
    hierarchy sheet's enumerated rack column is `rack_form_factor` → `Rack.form_factor`; NetBox 4.2
    gave the name `rack_type` to a foreign key pointing at a different model
  - `Conflict` enum (`CONSEQUENTIAL` / `DESCRIPTIVE` / `IDENTITY`) as a required field on every spec,
    populated from the D8 table in `design.md`
  - Choice tuples drive the xlsx picklists and the example rows only; validation still asks the API,
    so a choice list going stale cannot silently reject a value NetBox accepts (Requirement 7.7)
  - Synthetic example values only — the example region is `Example Region`, the site `EXA1`
  - **Extended by task 18a (D9):** the hierarchy sheet gains `datacenter_slug` and `datahall_slug`
  - Tests: required and optional columns match the requirements tables; the D8 split matches the
    design table exactly; every column carries a `Conflict` and help text; no example value looks
    like production data; `transceiver` is absent (D3) and `rack_type` is not a column name
  - _Requirements: 4.3, 4.6, 11.5c, 7 (columns), 8 (columns), 9 (columns)_

- [x] 6. Template generation — **done**
  - `.csv` and `.xlsx` generated from `ColumnSpec`: header on line 1, guidance on line 2, one
    synthetic example row on line 3. xlsx fills optional headers differently from required ones and
    adds a picklist per enumerated column
  - Six download URLs, named `<page>_template_csv` / `<page>_template_xlsx` — `design.md` lists one
    `<page>_template` name, which cannot address two formats
  - Each importer page now lists its columns, required-ness, and guidance from the same specs
  - Tests: round-trip in both formats and over HTTP — a generated template's only complaint is the
    deliberate example-row refusal, which proves headers matched and the guidance row was skipped
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.7_

- [x] 7. Upload parsing — **done**
  - Extension and content-type allow-list, size cap before parse, `.xlsx` read-only with values not
    formulas, first-or-named worksheet, case-insensitive whitespace-trimmed header matching, any
    column order, blank-row skipping
  - Reject the file listing all missing required columns at once; warn on unknown columns; delete
    temp files in `finally`; never write to a served path
  - Skip the template's guidance row (line 2, recognised by its marker) so a template can be filled
    in and uploaded exactly as downloaded (Requirement 5.11)
  - Refuse rows still holding the template's example values
  - `SheetRejected` carries every issue from the stage that failed plus the stage name, so a
    structural failure stops before later stages emit misleading errors (Requirements 10.3, 10.4)
  - An `.xlsx` is checked for its zip magic bytes as well as its extension, and a workbook that
    fails to open reports a plain sentence rather than an openpyxl traceback
  - Tests: 51 covering each criterion, including headers reordered and re-cased, all missing columns
    listed at once, a duplicated column, an unknown column reported as INFO, blank rows skipped
    mid-file, numeric cells not arriving as `45.0`, a formula cell never evaluated, a markup cell
    carried as text, and the sha256 fingerprint changing with the file
  - _Requirements: 5.1–5.11, 4.5_

## Phase 4 — NetBox access

- [x] 8. NetBox client factory — **done**
  - `services/netbox_client.py`: `build_client` from settings, verify-SSL and a connect/read timeout
    applied through a session wrapper, `/api/status/` probed once per process and the version cached
  - `classify()` maps a raw exception to DNS / TLS / TIMEOUT / AUTH / CONNECTION / VERSION / SERVER,
    each with an operator sentence naming the next action; `redact()` runs over every message before
    it can reach an exception, a log record, or a template
  - Writes go through `create()`, and the session refuses any method outside GET / OPTIONS / POST, so
    the create-only invariant is enforced at the transport rather than trusted per importer
  - Deviation from the plan: raw `pynetbox` is not used at request level. `pynetbox` raises
    `RequestError` with the response body interpolated into `str(e)`, which is exactly what task 21
    must guarantee cannot happen; requests go through this module's own session so redaction has a
    single choke point
  - Added while building task 12: `count()` asks for `?limit=1` and reads `count`, so readiness and
    S4 can size an endpoint without paging every record
  - Tests: 40 in `test_netbox_client.py` — each failure class maps correctly, the version is captured
    and parsed, the token is absent from exception text and log records, and a PATCH/PUT/DELETE
    attempt raises before a request is issued
  - _Requirements: 2.5, 2.6, 12.7, 14.1, 14.5_

- [x] 9. Lookup and choice cache — **done**
  - `services/lookups.py`: `prefetch`, `get_or_none`, `all_records`, `count`, `choices_for`,
    `prefetch_terminations`, `terminations_for`, `occupied`, and `add_records` so a just-created
    object is visible to later rows without a refetch. One instance per validation run
  - Two NetBox behaviours confirmed on 4.6.8 shaped this module and are recorded in its docstring:
    an unrecognised query parameter is **ignored** and returns every object with HTTP 200, and
    filtering by a deleted id returns **400** rather than an empty list. Server-side filters are
    therefore treated as a bandwidth hint only — every record is re-verified locally against the
    natural key before it counts as a match, so a mistyped filter name cannot become a false match
  - Tests: 19 in `test_lookups.py` — repeated lookups hit the API once, choices come from OPTIONS
    rather than a hardcoded list, and the request count stays flat as the row count grows from 1 to
    500 rows
  - _Requirements: 11.1, 15.1, 15.2_

- [x] 9a. Target readiness check — **done**
  - `check_version` enforces `>=4.6, <4.7`, naming both the detected and the supported version, and
    `connection_status` turns the probe into an ok / degraded / unusable banner state
  - ~~Startup validation of `NETBOX_URL`~~ — **done in task 2**: `config/env.py` requires `https://`
    unless the host is loopback, refuses plaintext to any remote host, and logs and banners the
    loopback development case
  - `readiness_for(importer, client)` returns read-only facts per page: the default device role
    resolves or does not, how many device types and manufacturers exist, whether any device has
    front/rear ports. GET and OPTIONS only — the module has no path that writes
  - Rendered as a panel on each importer page, so an empty instance says what sheets 2 and 3 need
    before an upload rather than after every row fails
  - Tests: 18 in `test_readiness.py` — each out-of-range version refused, readiness issues zero
    writes, the missing default role warns at page load, and an unreachable NetBox degrades the page
    instead of raising
  - _Requirements: 2.3a, 2.3b, 2.7, 14.5, 17.2, 17.3, 17.4_

- [x] 10. Rack naming module — **done**
  - `naming.py`: `parse_rack_name` / `build_rack_name` over `RACK_NAME_DELIMITER`, exactly three
    non-empty segments, and `RackName.agrees_with` for the sheet-versus-name check in task 13
  - `derive_slug` lives here too rather than in each importer: NetBox requires a slug on regions,
    sites, locations, device types, and manufacturers, no sheet asks for one, and two importers
    deriving it differently would disagree about which existing object a name refers to
  - Tests: 22 in `test_naming.py` — two and four segments rejected, empty segment rejected, an
    alternate delimiter honoured, and comparison case-insensitive while the supplied casing is
    preserved in what gets created
  - **Superseded in part by task 18a (D9):** the sheet does now ask for two of the slugs, the rack
    name's first two segments became slugs rather than names, and this module gained the slug
    validation rules that go with that
  - _Requirements: 6.1, 6.2, 6.4, 6.5_

## Phase 5 — Validation framework and importer pipeline

- [x] 11. Validation primitives in `validation.py` — **done** (partly landed with task 7)
  - **Landed with task 7:** `Severity`, `ValidationIssue` (file, sheet, line, column, value,
    expected, code, message, related line/object/url) with `location()`, the `Code` registry, `RowError`
    converting straight into an issue, and `IssueCollector` so a stage reports everything it found.
    Brought forward because a parser with no issue type has nowhere to put "these four required
    columns are missing"
  - **Landed here:** `Verdict.of(...)` with `errors` / `warnings` / `infos`, `stages_not_reached` and
    `unreached_explanation()`, a 30-minute `expires_at`, `needs_acknowledgement` / `acknowledge`, and
    `refusal_reason()` so a refusal states which of the five conditions failed rather than just
    disabling a button. `is_applyable()` is the single gate every caller asks
  - Reusable S2 cell checks (`require_value`, `positive_integer`, `one_of`) so three importers cannot
    word the same rejection three ways
  - `compare_to_existing(spec, sheet_value, existing)` implements D8: exact for identifiers, slugs,
    and choices, numeric for the columns marked `numeric` (so `19` and `19.0` from NetBox agree),
    trimmed/collapsed/case-folded for free text, at the severity the column's `Conflict` dictates and
    silent when the values agree. `numeric` became an explicit `ColumnSpec` field rather than being
    inferred, because guessing from the value would make `bottom_ru` and a numeric-looking label
    behave differently by accident
  - Tests: 56 in `test_validation_gate.py` — `is_applyable` false with one error, false with
    unacknowledged warnings, false when expired, false when a stage was not reached, false on a
    second apply, true only when genuinely clean; `location()` renders `file / sheet / line / column`;
    `spine row A` vs `Spine Row A` is not a conflict while `19` vs `23` on a consequential column is
    an error and a differing description is a warning
  - _Requirements: 10.5, 10.8, 10.9, 11.5, 11.5a, 11.5b, 16.1, 16.2, 16.3, 16.4_

- [x] 11a. Verify NetBox bulk-create atomicity (decision D7) — **spike, done 2026-08-18**
  - **Finding: bulk create is atomic on NetBox 4.6.8.** A field-level error, a deferred database
    uniqueness violation raised mid-write, and a model `clean()` error each rolled the entire list
    back. Full method and results recorded in `design.md` under "Measured: bulk create is atomic"
  - Follow-on for task 12: group creates by endpoint and issue one bulk POST each; on a bulk failure
    whose error body is *not* a per-item array, replay the list row by row to attribute the error to
    a spreadsheet line (safe because the bulk attempt wrote nothing)
  - _Requirements: decision D7, 12.4_

- [x] 12. Shared pipeline in `importers/base.py` — **done**
  - `RowOutcome`, `PlannedRow` (keyed by spreadsheet line), `CreateAction`, `Ref` + `Registry` for
    parent ids that do not exist yet, `Plan`, `RowResult`, and `RunReport` with a formula-escaped CSV
  - Deviation from the plan: rather than an `Importer` protocol with three entry points, `BaseImporter`
    owns one `validate()` that runs S2–S5 and calls four narrow hooks — `extra_cell_checks` (S2),
    `check_consistency` (S3), `prefetch` and `plan_row` (S4/S5). Three importers implementing a
    three-method protocol would have each re-implemented stage ordering and the stop-early rule, which
    is precisely the logic Requirement 10.2 wants in one place. Only `plan_row` is mandatory; the rest
    default to doing nothing
  - `apply` refuses unless `verdict.is_applyable()`, re-runs S4 first and aborts with zero writes if
    anything moved, then walks actions in `endpoint_order` recording a sequence number per row
  - Per D7, creates are grouped by endpoint and sent as one bulk POST each; a failed batch wrote
    nothing, so its rows are marked `BLOCKED` and later endpoints never start. When the error body is
    a single un-indexed object rather than a per-item array, the batch is replayed row by row to
    attribute it to a line — the replay is logged and the report says the endpoint was applied
    individually, rather than implying it stayed atomic
  - `NetBoxUnavailable` during the pre-write re-check is caught and reported as a clean abort. Without
    it, NetBox going away between validation and apply raised out of the drift check — an
    indistinguishable-from-a-crash path in the one place the operator most needs to know nothing was
    written
  - Tests: 57 in `test_hierarchy_importer.py` exercise this shared pipeline through a concrete
    importer — a plan with one error issues zero HTTP writes, stale S4 aborts before the first write,
    auth failure aborts, a failed batch blocks every row in it and halts later endpoints, an
    un-indexed error triggers the replay and lands on the right line, and NetBox disappearing before
    the first write aborts cleanly
  - `importer/tests/fakes.py` holds `FakeNetBox`, an in-memory double that reproduces the API quirks
    that matter: ignored query parameters, atomic list POSTs, choices cached per endpoint, and
    injectable read or write failures. Tests run in ~5s with no HTTP at all, and
    `importer/tests/conftest.py` fails any test that tries to open a real socket
  - _Requirements: 10.1, 10.2, 10.4, 10.6, 10.7, 10.14, 11.1, 11.2, 12.3, 12.4_

- [x] 13. Hierarchy importer — **done**
  - Region → site → location → rack in that `endpoint_order`, slugs derived via `naming.derive_slug`,
    parents referenced by `Ref` until the batch that creates them returns ids
  - `rack_u_height` validated, `rack_width` defaults to 19 and `rack_status` to `active`,
    rack-name-versus-columns agreement enforced through `RackName.agrees_with`, in-file duplicate
    racks rejected naming both lines, and the same datahall name under two datacenters kept distinct
  - Relationship conflicts are checked explicitly rather than through `compare_to_existing`: the
    `location` / `datacenter` / `datahall` columns are IDENTITY (a different datacenter name is a
    different site, not a wrong one), but Requirement 11.5 also calls a site's region and a location's
    site consequential, and a relationship is not a column. A site found in a different region blocks
  - A name that reduces to an empty slug reports `SLUG_NOT_DERIVABLE` rather than the collision code
    it first shared — the operator's fix is different, so the code should be too
  - The only importer that creates its own parents, so it works against a completely empty NetBox
  - Tests: 57, one per criterion in Requirement 7, including a slug collision against a different
    object and the D8 split on a site in the wrong region (blocks) versus one whose description alone
    differs (warns)
  - **Superseded in part by task 18a (D9):** slugs for the datacenter and datahall now come from the
    sheet when supplied, the rack-name agreement check compares slugs rather than names, and the two
    slug columns are `CONSEQUENTIAL` where the three name columns remain `IDENTITY`
  - _Requirements: 7.1–7.9, 6.3, 11.4, 11.5, 11.5a_

- [x] 14. Device importer — **done**
  - Rack resolved from the sheet, face validated against API choices, and `bottom_ru` through
    `bottom_ru + ru_height - 1` checked three ways: against the rack's own height, against the other
    rows of the file (S3), and against the devices already mounted on that face (S4). Every device in
    every rack the sheet touches is prefetched, so overlap detection does not cost a request per row
  - `ru_height` is never submitted — NetBox takes height from the device type — so the column exists
    to cross-check that type and to size one that autocreate creates
  - Device type resolved, or created with its manufacturer only when `NETBOX_AUTOCREATE_DEVICE_TYPES`
    is on (off by default, D4); role falls back to `NETBOX_DEFAULT_DEVICE_ROLE` and the row is
    rejected when neither resolves; serial, asset tag, tenant, and status optional
  - An asset tag already held by a *different* device blocks; held by the same device it is not a
    conflict. An existing device name is skipped and resolves nothing further — nothing will be
    written for that row, so resolving prerequisites it does not need could only report problems twice
  - Distinct codes rather than reusing near-enough ones: `DUPLICATE_ASSET_TAG_IN_FILE`,
    `MANUFACTURER_REQUIRED_FOR_AUTOCREATE`, `DEVICE_TYPE_WILL_BE_CREATED` (a warning the operator
    should see before junk device types appear in a shared instance)
  - Tests: 66 in `test_device_importer.py`, one per criterion in Requirement 8, including two rows
    colliding on RU naming both spreadsheet lines
  - _Requirements: 8.1–8.12, 16.6_

- [x] 15. Cabling importer — **done**
  - Each end resolved by kind (`device` → interface, `panel` + side → front port unless the side says
    rear), with unresolvable ends and ports rejected naming the end. A panel is an ordinary NetBox
    device, so both kinds are found the same way and only the port model differs
  - One bulk POST to `dcim.cables` with `a_terminations` / `b_terminations` as
    `{object_type, object_id}` arrays. The read-only cable-terminations endpoint is never touched, and
    a single endpoint means the whole sheet is one transactional batch with no half-applied window
  - `cable_type`, `cable_status`, and `length_unit` validated against API choices, status defaulting
    to `connected`; `length` requires its unit; `color` must be a six-digit hex
  - A row whose termination already carries a cable is skipped naming that cable; same-port-both-ends
    and a termination reused across two rows are both rejected
  - Spec correction: Requirement 9 listed `a_end_kind` / `b_end_kind` as required while 9.1 gave them
    a default of `device`. Those cannot both hold — S2 rejects a required column's blank cell before
    any default can apply — so the columns are now optional and the default is reachable and tested.
    `requirements.md` and `design.md` were corrected to match
  - No transceiver handling (D3): a `transceiver` column in an uploaded file is an unrecognised
    column, reported as informational and dropped, and no interface's `type` is ever written
  - Tests: 71 in `test_cabling_importer.py`, one per criterion in Requirement 9, asserting the exact
    payload shape, plus the assertion that the importer issues no `PATCH`, `PUT`, or `DELETE` at all
  - _Requirements: 9.1–9.12, 11.3_

## Phase 6 — Operator flow

- [x] 16. Validate endpoint and issue report UI — **done**
  - `/<page>/validate/` takes the upload, runs S1–S5 through `services/runs.validate_upload`, and
    redirects back to the page. POST-redirect-GET, so refreshing cannot re-upload
  - The plan is held in the cache under a user-scoped key and the browser holds only a run id
    (`services/plan_store.py`), because a plan is a graph of dataclasses and Django serialises
    sessions as JSON — a hand-written codec's bugs would be silent and would mean applying something
    subtly different from what was validated. Consequence recorded in the module docstring: the
    default cache is per-process, so a multi-worker deployment needs a shared backend; the failure
    mode is a plan reported as missing, which writes nothing
  - `_verdict_banner.html` states rows *and* objects: `Verdict.object_count` was added because one
    hierarchy row creates four objects, and "1 row" and "4 objects in NetBox" are different consents.
    It counts distinct `(endpoint, key)` pairs, so a datahall shared by twenty rows counts once
  - `_issue_list.html`: per-code tally first, then severity → line groups, each issue rendering
    location, received, expected, message, the other line for duplicates, and a NetBox link
  - Display cap (`MAX_ISSUES_DISPLAYED`, default 500) with a truncation notice, and errors are capped
    last so a flood of infos can never crowd out the three blocking problems
  - Three validation outcomes share one shape: a rejected file and an unreachable NetBox both produce
    a verdict with no plan, rather than a second rendering path
  - Added a target check missing from the earlier phases: an unreachable *or out-of-range* NetBox
    refuses the file before it is parsed (Requirement 2.7 was previously only a disabled button), with
    a new `NETBOX_VERSION_UNSUPPORTED` code distinct from `NETBOX_UNREACHABLE`
  - Tests: 47 in `test_validate_view.py` and 25 in `test_reports.py` — zero writes for clean, blocked,
    and unreadable files; stages-not-reached stated; every localisation field present; sheet content
    escaped rather than rendered; truncation notice at the cap with the CSV still complete
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.10, 16.1–16.11_

- [x] 17. The apply gate — **done**
  - Apply is POST-only (the view has no `get`, so the verb check is structural rather than a
    condition) and CSRF-protected. Its first act is `verdict.refusal_reason()`, returning 409 with
    that reason — the absent button is not the control
  - A blocked file renders no apply form at all: absent rather than greyed out, because a disabled
    control invites working out how to re-enable it
  - Acknowledgement, fingerprint binding, 30-minute expiry, and the second-apply refusal are all one
    check, because they are all `Verdict.refusal_reason()` — the view cannot disagree with the pipeline
  - Applying redirects to `/runs/<id>/`, so the report renders from the persisted record and a refresh
    cannot re-submit. A partly applied plan is marked applied for the same reason a fully applied one
    is: resuming mid-sequence without re-validating is the failure this gate exists to prevent
  - `static/js/apply.js` is the only JavaScript in the app — a busy state so a double submission is
    never attempted. With JS off the form still works and the server still refuses the duplicate
  - Tests: 30 in `test_apply_gate.py`. A forged POST on a blocked plan, a POST naming a good line, a
    mismatched fingerprint, an expired verdict, unacknowledged warnings, an unsupported version at
    apply time, and a second apply are each refused with `netbox.posts == []`. NetBox gaining an
    object between validation and apply aborts with zero writes; so does NetBox becoming unreachable.
    Logout drops the held plan while the run record survives
  - _Requirements: 10.6–10.9, 10.11–10.17, 3.3, 1.5, 14.3, 15.4_

- [x] 18. Audit log and reporting — **done**
  - `ImportRun`, `ImportRowResult`, and `ValidationIssueRecord` in `models.py` with migration 0001.
    No token, file, or credential field exists to leak
  - Every validation is a run, blocked ones included, with verdict, stage list, counts, fingerprint,
    and issues. `verdict` gained `aborted` and `unavailable` beyond the states `design.md` listed:
    "aborted partway with writes" and "refused because the target was unusable" are different facts
    from `stale`, and an operator unwinding by hand needs to tell them apart
  - `ImportRowResult` stores the NetBox `url` rather than rebuilding it from `NETBOX_URL`, so a link
    in an old report still resolves after the tool is repointed. `sequence` is the write order, which
    is what a manual unwind reads backwards; it is zero for rows that wrote nothing
  - Read-only `/runs/` and `/runs/<id>/`. Read-only means no route: POST, PUT, and DELETE on the
    detail URL are 405 because nothing is registered behind them
  - `reports.py` owns both CSVs and the one `escape_cell` they share, so the formula guard cannot be
    forgotten on a new column. `RunReport.to_csv` was refactored onto it, which is how the apply report
    gained escaping it did not have
  - Requirement 12.5's guarantee is rendered on the report itself, not left to the documentation
  - Tests: 29 in `test_run_history.py` — a rejected batch attributed to its line and column, an abort
    naming the endpoints that completed, a disclosed row-by-row replay, the token absent from every
    persisted field and every rendered page, and no mutating route
  - _Requirements: 12.1, 12.2, 12.5, 12.6, 13.1–13.5_

- [x] 18a. Sheet-supplied slugs, and rack names built from them (D9) — **done**
  - Revises tasks 5, 10, and 13. The rack name's first two segments become the datacenter and datahall
    **slugs** rather than their names, and the hierarchy sheet gains optional `datacenter_slug` and
    `datahall_slug` columns to supply them. Until now the slug was derived from the name, which forced
    the name to double as the short code: a site had to be called `SYD1` rather than "Sydney One" purely
    to keep rack names short
  - `columns.py`: two new `ColumnSpec`s placed immediately after the name column each belongs to, so a
    template reads name, slug, name, slug. Both `CONSEQUENTIAL`, not `IDENTITY` — `IDENTITY` means
    "cannot differ", which stops being true the moment an operator types the value themselves
  - `naming.py`: `validate_slug` and `effective_slug`; `RackName.agrees_with` takes slugs; `PATTERN`
    becomes `<datacenter_slug>-<datahall_slug>-<rack>`. Two separate checks, not one regex — outside
    NetBox's charset and containing-the-delimiter are different mistakes with different fixes, and the
    delimiter check reads `RACK_NAME_DELIMITER` rather than matching a literal `-` so Requirement 6.5
    survives
  - `validation.py`: `SLUG_INVALID`, `SLUG_CONTAINS_DELIMITER`, and `SLUG_INCONSISTENT_IN_FILE`
    (one name given two slugs). `SLUG_COLLISION` and `SLUG_NOT_DERIVABLE` keep their current meanings
  - `importers/hierarchy.py`, five places: `extra_cell_checks` validates the effective slug per level;
    `check_consistency` compares the rack name against slugs and gains both directions of the slug/name
    consistency check, with datahall slugs keyed by site per Requirement 7.9; `prefetch` loads by
    effective rather than derived slug; the three level resolvers put the effective slug in both the
    lookup and the payload; and `_slug_matches` splits into "this slug belongs to another object"
    (existing) and "this object's slug is not the one you asserted" (new, blocking)
  - The supplied-versus-derived distinction is the crux and belongs in one place: a **supplied** slug
    that disagrees with an existing object blocks (7.13), a **derived** one does not (7.14). Applying
    the blocking rule to the fallback would make every pre-existing object whose slug this tool did not
    derive start rejecting rows that pass today
  - A derived slug containing the delimiter — `derive_slug("Data Hall 1")` is `data-hall-1` — is
    rejected against the **name** column, not the blank slug column, because that is where the fix goes
  - Fixtures and docs move with it: `samples/1_hierarchy_*.csv`, `scripts/make_sample_sheets.py`, the
    example row in `columns.py`, and the header lists asserted in `test_columns.py` and `test_sheets.py`.
    At least one sample should carry multi-word names so the feature is exercised by the samples
  - Tests: extend `test_naming.py` (charset, underscore accepted, hyphen rejected, over-length,
    supplied-versus-derived precedence, and an alternate delimiter flipping which character is banned)
    and `test_hierarchy_importer.py` (supplied slug reaches the payload verbatim, blank falls back,
    derived-with-delimiter reported on the name column, one name with two slugs, two names with one
    slug, the same datahall slug legal under a second datacenter, supplied disagreement blocks, derived
    disagreement does not). A regression test that a pre-D9 single-token sheet still validates unchanged
    is the one that proves the fallback works
  - _Requirements: 6.3, 6.4, 6.6, 6.7, 7.3, 7.10–7.14, 11.1, 11.5, D9_

  **How it came out.**
  - The **region deliberately gained nothing**. Its slug is still derived and still only has to exist,
    because it appears in no rack name — so a region may keep a hyphenated slug like `lab-region` that
    the same rule would forbid for a site. The delimiter ban is a rack-name constraint, not a NetBox one,
    and applying it where no rack name is involved would have rejected files that are fine
  - `validate_slug` grew two wording-only arguments beyond the design's sketch, `derived_from` and
    `advice`, so that all slug phrasing stays in the one function. The alternative — catching the error
    in `effective_slug` and re-raising it with a different message — would have written every slug
    message twice, in two places that could then disagree
  - `row_slug` and `blame_column` are module-level in `importers/hierarchy.py` rather than methods,
    because S2, S3, S4, and `prefetch` all need the same answer. Computing the effective slug once per
    level in `plan_row` and passing it down is what makes Requirement 7.10 structural: the lookup, the
    payload, and the rack-name check cannot be handed different values, because there is only one
  - `_supplied_slug_matches` is a wrapper around `compare_to_existing`, not a new comparison. That is
    what makes the supplied-versus-derived distinction free: `compare_to_existing` already returns
    nothing for a blank cell, so a derived slug never reaches the check, and the column's CONSEQUENTIAL
    classification is what turns a real disagreement into a blocking error
  - The datahall gained the collision guard it never had. Looking a location up by `(site, slug)` and
    finding a *differently named* room used to be treated as a match, which would have attached the
    row's rack to the wrong datahall; it now blocks, scoped to the site per Requirement 7.9
  - Slugs compare case-insensitively, following Requirement 6.4 rather than the design's "slugs match
    exactly": `EXA1-DH1-R01` has to agree with the slugs `exa1` and `dh1`, and a comparison that folded
    for the rack name but not for the slug behind it would contradict itself. What is *created* still
    carries the casing the sheet gave (assumption 8)
  - The samples exercise both halves: the first three rows of `1_hierarchy_valid.csv` leave both slug
    cells blank, which is the pre-D9 sheet, and the fourth carries "Lab Two" / "Data Hall One" with
    supplied slugs and the rack name `LAB2-DH1-R01`. `1_hierarchy_invalid.csv` gains the mistake this
    change makes possible — a multi-word name with the slug cell left blank — which reports against
    `datacenter`, not against the cell the operator never filled in
  - Tests: 697 in the suite, up from 659. 47 in `test_naming.py` (13 new, covering the charset,
    over-length, the delimiter ban, and the alternate delimiter flipping which character it bans) and
    70 in `test_hierarchy_importer.py` (13 new in `TestSuppliedSlugs`). Two of those are the ones worth
    keeping honest: a sheet whose slug columns are *absent from the header entirely* still validates,
    and a derived slug that disagrees with an existing object still does not block

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
  - The slug columns of D9: what they are for, that `-` is not allowed inside one, and that a
    multi-word datacenter or datahall name now needs its slug column filled in
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
