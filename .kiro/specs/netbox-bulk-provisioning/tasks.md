# Implementation plan — NetBox bulk provisioning (iteration 1)

Dependency-ordered. Each task is small enough to review on its own and ends in a verifiable state.
Tests are written with the task they cover, not batched at the end. No task contacts a live NetBox
instance; HTTP is stubbed.

Phases 1–7 are iteration 1 and are complete. **Phase 8 is an addition on top of it** — cable profiles
and breakout, decision D10 — and is gated on that decision being accepted at review. Task 23 is the one
exception to the no-live-NetBox rule above: it verifies the API against a real instance, which is how
every other field in this spec was pinned down. **Phase 9 is the next addition** — loopback and
management addresses on the device sheet, decision D11 — gated the same way; task 27a is the
read-only probe.

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
  - **To be extended by tasks 24–25 (D10, proposed):** the cabling sheet gains `cable_profile` and
    `cable_group`
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
  - **One row, one cable, one termination per side** — which is what phase 8 revisits: that shape
    cannot express a breakout, because the rows sharing the fanout's A port are rejected by 9.11
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

- [x] 19. Row-count bound and progress
  - Enforce a documented maximum row count per apply as an S5 check, telling the operator to split
    larger files rather than timing out mid-run; show progress or a busy state while applying
  - Test: a sheet over the limit is blocked with the limit stated and nothing created
  - _Requirements: 15.3, 15.4_

  **How it came out.**
  - The mechanism was already there from task 11: S5 reads `MAX_APPLY_ROWS` and `apply.js` puts the
    button into a busy state. What was missing was **telling the operator before they upload** — the
    limit was discoverable only by tripping it. Both bounds now render on every page's upload panel,
    with the advice to split attached, so the number and what to do about it arrive together
  - The bound counts **rows, not objects**, and there is a test asserting that on purpose. Rows are
    what the request's cost tracks; counting objects would refuse a small sheet that happens to create
    a deep hierarchy, which is the opposite of the intent
  - A test at exactly the limit guards the off-by-one, because a limit stated in the UI that refuses
    the file it invited would be worse than no limit
  - The scale test measures the **request count for 2,000 rows** rather than wall-clock time: under 40
    requests where one-per-row would be 2,000. That is the property that makes the 2,000 default a
    policy about request duration rather than a symptom of a slow implementation. It runs against the
    importer directly, since going through a view would add the page probe's requests and obscure it
  - Tests: 10 new in `test_scale_and_limits.py`
  - Also fixed here: the upload panel still described rack names as `<datacenter>-<datahall>-<rack>`,
    which D9 had made wrong. Template text escapes its angle brackets, so the earlier sweep for the
    old wording missed it

- [x] 20. Gate integration test — the one that matters most
  - End-to-end per importer against a stubbed NetBox: upload a file with one bad row, assert the
    response is blocked, the issue names the right file/sheet/line/column, and **zero write requests
    were issued**; then fix the row, re-upload, and assert the apply succeeds
  - Repeat for a file that is clean at validation but whose stubbed NetBox state changes before
    apply: assert the abort happens with zero writes
  - _Requirements: 10.1, 10.6, 10.7, 10.14, 16.1_

  **How it came out.**
  - The three sheets are described as **data** — a good pair of rows, one deliberately broken row, the
    correction, the column the issue must name, and the drift to apply — and one parametrised fixture
    runs the same seven tests over all three. A per-importer copy of each test would have been three
    chances for the assertions to drift apart, which for the gate is the one thing that must not happen
  - **This found two real defects**, which is the argument for writing it:
    - `FakeNetBox` did not reproduce NetBox marking a termination as occupied when a cable is created.
      Every cabling test that needed occupancy had been setting `port["cable"]` by hand, so the
      *end-to-end* claim — re-upload a cabling sheet and nothing is created — was untestable and, in
      the double, false. The double now does what NetBox does, and the one unit test that asserted
      interfaces are untouched had to be narrowed to exclude `cable`, since occupancy is NetBox's
      bookkeeping rather than a write this tool issues
    - the first drift scenarios were unrelated objects, and they did **not** trip the drift check —
      correctly, because `plan.state` records only what the plan depends on. Rewritten as the races
      that actually happen: somebody else mounts a device in the U the plan wants, somebody else
      patches the port the plan wants. Both abort with zero writes
  - The refusal tests assert on `netbox.posts == []` rather than on stored objects, because an empty
    write log is the only evidence that distinguishes a refusal from a rollback
  - Tests: 27 new in `test_gate_integration.py` (nine per importer)

- [x] 21. Security regression suite and deploy check
  - Assert the token appears in no rendered page, log record, exception, or audit row, including on
    error paths; autoescaping verified with a cell containing markup
  - `python manage.py check --deploy` clean with `DEBUG=false`; confirm `.env` is git-ignored and no
    secret is present in tracked files or commit messages
  - Confirm every test fixture is synthetic
  - _Requirements: 14.1–14.7, 5.7, 12.7_

  **How it came out.**
  - The sweep is written against a **sentinel token that is token-shaped** — 40 hex characters, as
    NetBox issues — rather than the settings value, so a leak through a path that formats
    `Token <credential>` is unmistakable instead of invisible in a page of prose
  - **This found two leaks, both on error paths, and both fixed in the type rather than at the call
    site**:
    - `NetBoxWriteRejected` carried NetBox's error body verbatim into the abort reason, the audit rows,
      and the run report. A server or proxy that echoes request headers back in a 4xx body would have
      persisted the token. The body is now redacted on construction, walked recursively so the per-item
      shape that attributes an error to a row survives
    - `NetBoxUnavailable` relied on its *callers* having redacted the detail first. True for the
      classifier, not guaranteed for anything written later, so it now redacts its own message
    Both changes move the guarantee from "every construction site remembers" to "the type cannot
    produce an unsafe message", which is the only version that survives new code
  - `every_stored_value()` walks each run's fields and its related issues and results reflectively, so
    a **new model field** joins the sweep without anyone remembering to add it
  - Autoescaping is tested on both directions of untrusted input: a sheet cell containing `<script>`,
    which is quoted back by design because Requirement 16.1 asks for the received value, and a NetBox
    object whose name contains markup. There is also a check that no template uses `|safe` or
    `autoescape off` on operator text, which is the way this protection would actually be lost
  - `check --deploy` runs as a subprocess with `--fail-level WARNING` under a production-shaped
    environment, so a warning is a failure. Clean, including HSTS and the SSL redirect
  - The fixture-synthetic check is a **host and email allow-list** rather than a search for
    "suspicious" strings: every URL in the test tree must name an RFC 2606 reserved name, loopback, or
    `testserver`, and every email must be `example.com`. A real hostname in a fixture is the realistic
    way production detail arrives in a repository, and that is what this catches
  - Tests: 23 new in `test_security.py`

- [x] 22. README and operator runbook
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

  **How it came out.**
  - Split by **audience, not by topic**: `README.md` stays the engineer's entry point (setup,
    configuration, how the suite is built), `docs/operator-runbook.md` is for whoever fills the sheets
    in, and `docs/empty-instance-prep.md` is for whoever prepares NetBox once. Folding all three into
    the README would have buried the operator's material under the developer's
  - The operator runbook leads with the two facts that change behaviour — create-only, and one error
    blocks the whole file — because every other instruction depends on them. The D9 slug columns are
    documented as a decision table ("fill it in when the name is more than one word") rather than as a
    rule about characters, since that is the form the operator needs
  - **"A run aborted partway" is the section that mattered most** (Requirement 12.5), and its answer is
    that re-uploading the same sheet *is* the recovery path: everything already created is skipped, so
    the second run finishes the job. That is worth stating plainly, because the instinct on seeing a
    partial import is to delete what exists first
  - The prep runbook is ordered by dependency and says why: device types before devices, because NetBox
    does not retro-fit ports onto devices that already exist. It documents the 4.5+ `rear_ports` mapping
    with the actual payload shape, and points at `scripts/seed_netbox.py`, which already builds three
    panel layouts in that format — including the MPO panel whose front and rear ports share names, which
    is the reason the cabling sheet has a `b_port_side` column at all
  - `netbox-docker`'s quickstart was re-read rather than recalled. Its example override maps
    `8000:8080`, which is the reason this app serves 8080

## Phase 8 — Cable profiles and breakout (D10) — complete

All four tasks are done and D10's assumptions are measured rather than assumed: task 23a checked the
fields read-only, task 23b checked the behaviour by creating and deleting cables on 4.6.8, and every
expectation held, so no requirement needed amending. One thing is still taken on trust and is called
out in `design.md`: the internal position map of a `-shuffle` profile, which NetBox does not serve
over the API, so it cannot be checked against a harness specification from here.

- [x] 23a. Verify the cable-profile fields against a 4.6.8 instance — **done 2026-08-20**
  - `OPTIONS /api/dcim/cables/`: `profile` is present, writable, optional, and a plain choice field
    rather than a foreign key, with 26 values. Recorded in `design.md` alongside the other 4.6.8
    findings, together with the family-by-family connector table
  - `OPTIONS /api/dcim/cable-terminations/` returns `actions: []` — no write verb offered at all, so
    `connector` and `positions` cannot be set by any request and Requirement 9.21 stands
  - The `(\d+)C(\d+)P` parse of Requirement 9.19 was run against all 26 reported values: every one
    yields per-side connector and position counts, and the `-shuffle` suffix is invisible to it
  - Read-only throughout: `OPTIONS` and `GET` only, nothing created

- [x] 23b. Verify the behavioural facts — **done 2026-08-24, every assumption held**
  - Nothing here is observable without creating a cable, which is why it is a script rather than a
    test: `scripts/probe_cable_profiles.py`, which creates cables, reads them back, and deletes what
    it created. It is the only thing in the repository that issues DELETE — the app's HTTP layer
    refuses any verb outside GET/OPTIONS/POST, and the probe deliberately does not use it
  - Run it as `--url <target> --i-know-this-is-disposable`. `--url` is never defaulted, so the
    instance in `.env` cannot be hit by forgetting an argument, and it refuses an instance that
    already holds cables. Needs nine uncabled interfaces on distinct devices and creates no devices
    of its own; `docs/empty-instance-prep.md` covers standing an instance up
  - Four questions, and where each answer lands:

    | Question | If the answer is what we assumed | If it is not |
    | --- | --- | --- |
    | May a side carry **fewer** terminations than the profile has connectors? | Requirement 9.19's `<=` stands | It becomes `==` — one `<=` in `_check_group` — and a partly populated breakout stops being expressible at all |
    | Does NetBox **reject more** terminations than there are connectors? | Belt and braces with our own S3 check | Our check is the only guard; say so in `design.md` rather than implying NetBox backs it up |
    | Are connectors assigned in **submitted array order**? | Requirement 9.21 stands and the preview is honest | **Stop.** Row order as lane order is the whole of D10's answer to lane control; `a_connector` / `b_connector` become mandatory and `_describe_connectors` is currently a lie |
    | What does a `-shuffle` profile actually **map**? | Record it, so a harness can be checked against it | A profile whose map does not match the harness must not be used, and the runbook has to say so |

  - The fourth question was added after phase 8 shipped, when a real fabric turned out to need
    `trunk-4c4p-shuffle`: the shuffle map is NetBox's own, cannot be set, and a mismatch against the
    harness produces confident wrong traces with nothing to flag them. The probe prints the map and
    a path trace rather than interpreting either, because only the harness specification can settle it
  - The probe also posts a trunk with **four terminations on both sides**, which nothing had tried:
    every earlier check used a breakout's one-A-to-many-B shape
  - **Acceptance is a working per-lane trace, not a 201.** A 201 proves NetBox accepted the payload;
    tracing is the feature D10 exists to enable, so the trace output is the finding that matters
  - Then: record the answers in `design.md`'s "Still unverified" table, amend Requirements 9.19 /
    9.21 where reality disagreed, and tick this task
  - _Requirements: 9.19, 9.21, 9.22; assumption 7_

  **How it came out.** All five expectations held, so **no requirement needed amending** and the
  answers are recorded in `design.md` under "Measured: how NetBox behaves with a profile".

  - Fewer terminations than connectors is accepted, so 9.19's `<=` was right and a partly populated
    breakout is a real cable. More than the connector count is rejected — and precisely: `B side of
    cable has 5 terminations but only 4 are permitted for profile 1C4P:4C1P breakout`. **Our check
    is therefore belt and braces rather than the only guard**, which is worth knowing: its value is
    reporting at validation time instead of at apply time, not catching something NetBox misses
  - Connectors are assigned in submitted array order — four terminations came back as 1, 2, 3, 4 —
    so 9.21 stands and the preview's lane numbers are the ones NetBox will use. This was the answer
    that could have cost the most; it did not
  - `trunk-4c4p-shuffle` accepts four terminations on **both** sides, connectors 1–4 each way, each
    carrying `positions [1,2,3,4]`. The shape a leaf-spine shuffle fabric needs is expressible
  - **Per-connector tracing works; per-position tracing is not served.** A trace from one A-side
    interface of the shuffle returns all four far ends, which is right — that connector's four
    positions genuinely fan out — but nothing says *which* position reaches which. So a harness whose
    internal shuffle differs from NetBox's cannot be detected over the API, and a `-shuffle` profile
    stays an assertion. Recorded as the one thing D10 still takes on trust
  - **Two bugs in the probe itself, both of the kind that fake a result.** It first picked each
    device's first free interface, which is `br_default` — a bridge, which NetBox refuses to cable —
    so every probe failed for a reason unrelated to profiles *and the over-population probe passed*,
    because it only checked for a rejection and got one. Then the under-population probe reused the
    A interface of a cable still in place and was refused as a duplicate termination. Both are fixed:
    the fixture skips virtual/bridge/LAG types, each cable is dropped before its interfaces are
    reused, a failed baseline now skips the rest rather than reporting fiction, and every rejection
    prints NetBox's own message so "rejected" can be read rather than assumed

- [x] 24. `cable_profile` column — **done**
  - One `ColumnSpec` in `CABLING_COLUMNS`: optional, `Conflict.CONSEQUENTIAL` per the D8 table, with
    the 26 values recorded in `design.md` as the picklist and an empty example so the template's
    example row stays the common case
  - `choice_sources["cable_profile"] = (CABLES, "profile")`, which is the whole of the S2 check; and
    the 9.14 branch — a non-blank cell against an instance reporting no such choices is an ERROR
    naming the version, not a silently dropped value
  - `_cable()` adds `profile` only when the cell is non-blank, so an unprofiled cable's payload is
    unchanged byte for byte
  - `profile_structure(code)` parses `(\d+)C(\d+)P` tokens into per-side connector counts, returning
    None when it cannot; a test asserts every one of the 26 values task 23a recorded still parses
  - Tests: blank cell omits the key; a bad value is rejected listing the accepted ones; the template
    picklist offers the API's values; a target reporting no profiles blocks a named profile and still
    accepts a sheet that names none
  - _Requirements: 9.13, 9.14, 11.5, 4.4_

  **How it came out.**
  - `cable_profiles.py` is a new module beside `naming.py` rather than more surface on the cabling
    importer, because the 26 values are picklist material for `columns.py` *and* the parse is domain
    logic with its own test file. Nothing in it imports the rest of the app
  - **9.14 could not use the generic choice machinery.** `BaseImporter.choices` falls back to the
    sheet's own list when the API reports nothing for a field — deliberately, so a stale list cannot
    reject a value NetBox accepts — which means it cannot tell "no profiles at all" from "not one of
    them". So `extra_cell_checks` gained a `lookups` argument and the check lives there. Three
    importers implement that hook; the other two ignore the new argument
  - The `CONSEQUENTIAL` class on `cable_profile` is **declarative**: cabling never reaches
    `compare_to_existing`, because an occupied termination is a skip long before a field is compared.
    Exactly as true of `cable_type` today. Recorded in `design.md` rather than left to be discovered
  - Tests: 6 in `test_cabling_importer.py`, plus 71 in the new `test_cable_profiles.py` — the guard
    being that every one of the 26 probed values still parses into per-side connector counts

- [x] 25. `cable_group` — several rows, one cable — **done**
  - `group_key` / `plan_group` hooks on `BaseImporter`, defaulting to one group per row so hierarchy
    and devices are untouched; `CreateAction.line` becomes a tuple of lines
  - Cabling groups on the folded `cable_group`; terminations deduped per side in first-appearance
    order (9.16); 9.11 relaxed inside a group and 9.10 still enforced (9.17)
  - S3: attributes inherited from the group's first row with a contradicting non-blank later value
    rejected naming both lines (9.18); connector-count check against the profile (9.19); the
    no-profile-multi-termination warning (9.20)
  - S4: the occupancy tri-state of 9.22 — all free creates, all held by one cable skips every line,
    anything else blocks
  - Preview shows each row's connector number (9.21); the report records the created cable against
    every line of the group (9.23)
  - `FakeNetBox`: occupy every termination in a submitted array, not one per side, and report
    `profile` among the cable choices
  - New codes: `CABLE_GROUP_FIELD_CONFLICT`, `PROFILE_CONNECTOR_OVERFLOW`,
    `CABLE_GROUP_WITHOUT_PROFILE`, `CABLE_GROUP_PARTLY_CABLED`, `CABLE_PROFILE_UNSUPPORTED`
  - Tests: the design's four-row breakout example end to end, then re-uploaded to assert zero writes;
    the same block with `cable_group` removed producing one create and three duplicate errors
  - _Requirements: 9.15–9.23, 10.14, 11.3, 12.2, 16.6_

  **How it came out.**
  - **A third base hook was needed, and it is the one the design missed.** Requirement 9.18 lets a
    group state the cable's values once, but `cable_type` is a *required* column, so S2 rejected the
    blank cell on the later rows before any group-aware stage could look for the value. `normalise`
    runs before S2 and copies a group's cable-level values into its blank cells only — never over a
    non-blank one, so a genuine disagreement still reaches S3. Requirement 9.18 now says explicitly
    that "required" means required of the cable rather than typed on every row
  - **`created_count` had to change meaning**, from report lines to objects written. Four lines
    reporting one cable would otherwise have read as "4 created". It now counts results with a
    non-zero `sequence`, which already meant "this line wrote an object", so no new field was needed
    and the manual-unwind property of Requirement 12.4 is unchanged
  - `FakeNetBox` needed **less** work than planned: `_occupy_terminations` already looped over every
    entry in both termination arrays, so a four-lane breakout correctly occupies all five ports. The
    plan had assumed it occupied one per side. Only the `profile` choices were added
  - Groups are planned together and then re-sorted by line, so two interleaved groups still preview in
    sheet order (Requirement 10.18). Worth knowing that this makes row order load-bearing twice: for
    lanes, and for the preview
  - The occupancy tri-state of 9.22 is written so that **the pre-D10 behaviour of 9.9 is untouched**:
    "partly cabled blocks" applies only where a side has more than one termination. With one a side a
    skip loses nothing, which is why all 71 original cabling tests still pass unmodified
  - Tests: 46 new in `test_cabling_importer.py` (118 in the file), including the four-row breakout
    applied and re-uploaded, lane order following row order, the same rows without the tag producing
    three duplicate errors, and a partly cabled group blocking with zero writes

- [x] 26. Documentation and samples for breakout — **done**
  - `docs/operator-runbook.md`: when to fill the two columns in, that a group's later rows can be left
    blank, and that row order is lane order — with the plan preview as the place to check it
  - `samples/`: `cable_profile` stops being an ignored column, so the sample sheets need a real
    breakout group; `transceiver` stays as the unrecognised-column demonstration. Update
    `scripts/make_sample_sheets.py` accordingly
  - `docs/empty-instance-prep.md`: a note that tracing a lane *through a panel* also needs
    position-aware front-to-rear mappings, which is prep rather than import
  - _Requirements: 9.13–9.23, 5.6, 17.6_

  **How it came out.**
  - The runbook's breakout section leads with "a breakout is **one** cable, not several", because every
    other instruction in it follows from that. The five sub-points are the ones that actually catch
    people: blanks inherit, count far ends rather than fibres to pick the profile, row order is lane
    order and the preview shows it, cable the cage rather than the `swpNsM` children, and a half-built
    breakout is refused rather than skipped
  - **The samples are a new pair rather than edits to the old ones.** `CABLING_COLUMNS`' rows are
    fixed-width positional tuples with an optional trailing note, and `check_arity` cannot tell a note
    from a value — so adding a column would have meant re-indexing eighteen tuples with the silent
    shift that check exists to catch as the failure mode. `3_cabling_breakout_valid` /
    `_invalid` carry their own column tuple instead, and the original pair keeps its job of proving a
    sheet *without* the D10 columns is unaffected
  - Verified by generating them and validating both through the importer: the valid one is four rows,
    one cable, four B terminations, lanes 1–4; the invalid one is exactly three errors —
    `PROFILE_CONNECTOR_OVERFLOW`, `CABLE_GROUP_FIELD_CONFLICT`, `DUPLICATE_TERMINATION_IN_FILE`
  - **One old sample row had to change.** Its declared fault was "a multi-termination profile, which a
    row cannot express" — which was never true (the column was ignored, so the row had no fault at
    all) and is now doubly untrue, since NetBox allows a profiled cable to be partly populated. It
    carries a length without a unit instead, a fault no other row covered
  - `docs/empty-instance-prep.md` gained the switch-side half of the prep, not just the panel half: a
    device type defining both `swp1` and `swp1s0`/`swp1s1` gives a sheet author two places to land one
    cable, and the children want to be virtual with a `parent` or gone

## Phase 9 — Device loopback and management addresses (D11)

Gated on D11 being accepted at review, including the remaining operator question: whether a
management address should also be written to `oob_ip`. Dual-stack is decided: four columns, IPv4
preferred as primary unless the row supplies none. Do not start 27b until 27a has recorded the
probe results in `design.md`.

- [ ] 27a. Probe IPAM assignment and interface discriminators against 4.6.8
  - Read-only `OPTIONS` / `GET` on `/api/dcim/interface-templates/` (`mgmt_only`), the interface
    `type` choices (is `loopback` present), and `/api/ipam/ip-addresses/` (`assigned_object_type` /
    `assigned_object_id`, whether a parent prefix is required)
  - Confirm `Device.primary_ip4` / `primary_ip6` are writable on PATCH and not useful on create,
    and that two addresses of different families can share one interface
  - Record the answers in `design.md`'s D11 probe table; amend Requirements 8.16–8.19 if reality
    disagreed
  - _Requirements: 8.16, 8.17, 8.19_

- [ ] 27b. Dual-stack address columns
  - Four `ColumnSpec`s in `DEVICE_COLUMNS`: `loopback_ip4`, `loopback_ip6`, `management_ip4`,
    `management_ip6` — optional, `Conflict.CONSEQUENTIAL`, CIDR examples, no interface-name column
  - S2: a non-blank cell must be an `ipaddress.ip_interface` with an explicit prefix; a host with
    no mask is rejected, not defaulted; an `*_ip4` cell that is IPv6 (or the reverse) is
    `DEVICE_IP_FAMILY_MISMATCH`
  - S3: the same address in two cells — two rows, or two columns of one row — is
    `DUPLICATE_IP_IN_FILE` naming both locations
  - Template guidance states at-least-one of four on a type that defines the interfaces, and all
    blank on a type that does not
  - Tests: valid v4 in ip4 / v6 in ip6 accepted; `10.0.0.1` without a mask rejected; v6 in
    `management_ip4` rejected; duplicate in-file and same-row two-column duplicate blocked
  - _Requirements: 8.13, 8.22, 8.24, 8.25, 4.2, 11.5c_

- [ ] 28. Resolve interfaces from the device type, not from the sheet
  - Prefetch interface templates per device type touched by the sheet: unique `mgmt_only`, unique
    type `loopback`. For an existing device, resolve against the device's own interfaces by the
    same rules. Both families of one role assign to that same interface
  - S4: at-least-one of four when the type has either role (8.14); all-blank when it has neither;
    zero or more-than-one match on a filled column is `MGMT_INTERFACE_NOT_FOUND` /
    `MGMT_INTERFACE_AMBIGUOUS` or the loopback pair, naming the type and the candidates
  - An existing device missing the interface its type now defines is `DEVICE_INTERFACE_MISSING`,
    not an invitation to create the interface (8.23)
  - Plan preview names the resolved interface, each address, `primary_ip4`, `primary_ip6`, and
    which of those NetBox will show as primary (8.26)
  - Devices readiness panel counts types that define each role (17.2)
  - Tests: Arista-named `Management1` and Cumulus-named `eth0` both resolve as management from
    `mgmt_only` alone; two `mgmt_only` templates block; a patch panel with no such templates and
    all cells blank applies; a switch with the templates and all four cells blank is
    `DEVICE_IP_REQUIRED`; filling `management_ip4` against a type with no `mgmt_only` blocks
  - _Requirements: 8.14–8.16, 8.23, 8.26, 17.1, 17.2_

- [ ] 29. Create addresses and PATCH the primary IP
  - Apply order per row: create or skip the device, POST each supplied `ipam.ip-addresses`
    assigned to the resolved interface (up to four), PATCH the device with only `primary_ip4` /
    `primary_ip6` per 8.18 — family first (IPv4 unless the row has none), then management over
    loopback within the family
  - `CreateAction` (or a sibling) must be able to express that PATCH; cabling still issues none
  - Idempotency: address already on the intended interface is skipped; address held elsewhere or
    unassigned is `IP_ALREADY_ASSIGNED`; primary already set to a different address is
    consequential and blocks; primary unset is patched
  - An existing device still has its remaining address work done (8.10, 8.20) — this is the
    behaviour change from "skip the row and resolve nothing further"
  - `oob_ip` is not written unless D11's review says otherwise
  - Tests: new device gets IPs then primary; re-upload creates nothing; device-exists-IPs-missing
    creates the IPs and sets primary; IP held by another device blocks with zero writes; a row
    with `management_ip6` and `loopback_ip4` sets `primary_ip4` to the loopback and `primary_ip6`
    to the management address; a v6-only row sets only `primary_ip6`; grep for `PATCH` finds only
    this call, and the payload has no other keys
  - _Requirements: 8.17–8.21, 11.3, 11.6, 11.7_

- [ ] 30. Documentation and samples for device addresses
  - `docs/operator-runbook.md`: at-least-one of four, CIDR with a mask, names are on the type,
    IPv4 preferred as primary, patch panels leave all four blank
  - `docs/empty-instance-prep.md`: mark exactly one `mgmt_only` and exactly one type-`loopback`
    interface on each network device type; community YAML that types `Loopback0` as `virtual` must
    be corrected before sheet 2 will accept a loopback address
  - Samples: switch rows in `2_devices_valid.csv` gain CIDR values (at least one family); panel
    rows stay blank; the invalid sheet gains an all-blank switch, a panel with an address, a
    mask-less host, a v6 value in an ip4 column, and a duplicate address
  - `scripts/seed_netbox.py`: add `mgmt_only` and `loopback` templates for the switch types it
    already seeds, named for the `--naming` scheme
  - _Requirements: 8.13–8.26, 17.6_

## Deferred to iteration 2 (do not build now)

Update and delete of existing objects (other than the primary-IP PATCH of D11); IPAM beyond the
device sheet's loopback and management addresses (prefixes, VRFs, VLANs); multi-user roles and SSO;
background workers for very large sheets; multi-instance NetBox switching; compensating rollback of
applied creates. Dual-stack on the device sheet is in scope for D11 (four columns), not deferred.

**Transceiver recording (D3, D6).** Dropped from iteration 1 because setting `interface.type` cannot
hold a part number once device types define interface types. Iteration 2 should pick between a
`dcim.InventoryItem` bound to the interface and a module in a module bay, then build the part-number
map from the transceiver models actually in use. Note this reintroduces writes to objects the tool did
not create, so the create-only invariant and its test will need revisiting deliberately.

**Completing a partly cabled breakout (D10, Requirement 9.22).** Adding the missing lane to a cable
that already exists is a PATCH, so it lands with the update operations above rather than with phase 8.
Until then a half-cabled group blocks and says so. D11's primary-IP PATCH does not reopen this.
