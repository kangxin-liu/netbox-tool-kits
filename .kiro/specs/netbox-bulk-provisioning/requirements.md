# Requirements — NetBox bulk provisioning (iteration 1: create only)

## Introduction

A small Django web application that lets an authenticated engineer populate a NetBox instance
from spreadsheets. It connects to NetBox with a URL and API token taken from environment
configuration, and exposes three importer pages reachable from a shared nav bar:

1. **Site & rack hierarchy** — location, datacenter, datahall, racks
2. **Devices** — placed into racks by face and rack unit
3. **Cabling** — device and patch-panel terminations

Iteration 1 **creates** objects only. It never updates or deletes anything in NetBox. An object
that already exists is reported as skipped, which is what makes re-running a sheet safe.

Data classification: uploaded sheets and NetBox contents are **INTERNAL**; the NetBox API token
is **CONFIDENTIAL**. See `design.md` for the threat model and trust boundaries.

## Glossary and object mapping

The spreadsheets use the engineers' vocabulary. NetBox's DCIM model is the target:

| Sheet term | NetBox object | Notes |
| --- | --- | --- |
| Location | `dcim.Region` | Geographic grouping, e.g. a metro or country |
| Datacenter | `dcim.Site` | The physical facility |
| Datahall / room | `dcim.Location` | A child location scoped to the site |
| Rack | `dcim.Rack` | Assigned to both the site and the datahall location |
| Model | `dcim.DeviceType` | Matched by model name (and manufacturer when creating) |
| Panel | `dcim.Device` with front/rear ports | A patch panel is an ordinary device in NetBox |

**Naming convention.** A rack name is `<datacenter>-<datahall>-<rack>`, for example
`SYD1-DH1-R01`. This is the join key used by all three sheets.

## Requirement 1: Local admin-only access

**User Story:** As the tool owner, I want the app locked to a single local admin account, so
that only I can write to NetBox through it while the tool is still young.

### Acceptance Criteria

1. The system SHALL authenticate users against Django's local user database only, with no
 external identity provider.
2. WHEN an unauthenticated visitor requests any page other than the login page, THEN the system
 SHALL redirect to the login page without revealing any NetBox data.
3. The system SHALL grant access only to users with `is_superuser` set; WHEN an authenticated
 non-superuser requests any importer page, THEN the system SHALL return HTTP 403.
4. The system SHALL provide no self-service registration or password-reset flow; accounts are
 created with `manage.py createsuperuser`.
5. WHEN a user logs out, THEN the system SHALL clear the session and discard any pending import
 preview held for that session.
6. The system SHALL NOT ship any default, seeded, or hardcoded credentials.

## Requirement 2: NetBox connection configuration

**User Story:** As an engineer, I want the NetBox URL and token supplied by configuration and
its reachability shown to me, so that I know I am writing to the right instance before I import.

### Acceptance Criteria

1. The system SHALL read `NETBOX_URL` and `NETBOX_TOKEN` from environment variables and SHALL
 NOT accept either as user input in the web UI.
2. IF `NETBOX_URL` or `NETBOX_TOKEN` is missing or empty at startup, THEN the system SHALL fail
 to start with a message naming the missing variable and no partial functionality.
3. The system SHALL verify TLS certificates by default; IF `NETBOX_VERIFY_SSL` is false, THEN
 the system SHALL log a warning at startup and display a persistent banner in the UI.
3a. The system SHALL require an `https://` scheme for `NETBOX_URL` and SHALL refuse to start on
 `http://`, with one exception: a loopback host (`localhost`, `127.0.0.1`, `[::1]`) MAY use
 `http://` for local development. WHEN the loopback exception is in effect, THEN the system SHALL
 log it at startup and show a development banner naming the target.
3b. IF `NETBOX_URL` uses `http://` with a non-loopback host, THEN the system SHALL refuse to start
 and SHALL say that plaintext to a remote NetBox is not permitted, so that the exception cannot be
 stretched to a shared or production instance.
4. WHEN any page renders, THEN the system SHALL display the target NetBox hostname and the
 connection status, and SHALL NOT display the token or any part of it.
5. WHEN the app cannot reach NetBox, THEN the system SHALL disable the apply action and show a
 diagnostic that states the host and the failure category (DNS, TLS, timeout, auth) without
 including credentials.
6. WHEN a connection is established, THEN the system SHALL record the NetBox version reported by
 `/api/status/` for use in version-sensitive payloads and in the audit log.
7. IF the reported NetBox version falls outside the supported range (`>=4.6, <4.7`), THEN the system
 SHALL refuse to import and SHALL name the detected and supported versions, rather than failing
 partway through on a changed field name.

## Requirement 3: Navigation shell

**User Story:** As an engineer, I want the three importers as three pages with a shared nav bar,
so that I can move between them without losing my place.

### Acceptance Criteria

1. The system SHALL provide three distinct pages at stable URLs: hierarchy, devices, and cabling.
2. The system SHALL render one nav bar on every authenticated page linking to all three pages,
 and SHALL visually mark the current page.
3. WHEN a user navigates away from a page with an unapplied preview, THEN the system SHALL retain
 that preview so that returning to the page does not require re-uploading.
4. The system SHALL show the logged-in username and a logout control on every authenticated page.
5. The nav bar SHALL be usable at a 1280px-wide viewport without horizontal scrolling.

## Requirement 4: Downloadable templates

**User Story:** As an engineer, I want to download a correctly shaped template for each importer,
so that I do not have to guess column names or ordering.

### Acceptance Criteria

1. Each of the three pages SHALL offer a template download in both `.csv` and `.xlsx`.
2. The system SHALL generate templates from the same column definitions used by the parser, so
 that a downloaded template is always accepted by the importer that produced it.
3. Each template SHALL contain the header row, at least one example data row using clearly
 synthetic values, and per-column guidance (required/optional, format, allowed values).
4. The `.xlsx` template SHALL mark optional columns distinctly from required ones and SHALL
 present enumerated fields (rack face, cable type, panel/device kind) as picklists.
5. WHEN the example row is left in place and uploaded unchanged, THEN the system SHALL flag it as
 example data and refuse to import it rather than creating objects named after the examples.
6. Template downloads SHALL contain no real site, device, serial, or customer values.

## Requirement 5: Upload and parsing

**User Story:** As an engineer, I want to upload a filled-in CSV or XLSX and have it read
safely, so that a malformed or oversized file cannot break the tool.

### Acceptance Criteria

1. The system SHALL accept `.csv` and `.xlsx` uploads and SHALL reject all other extensions and
 content types with a message naming the accepted formats.
2. The system SHALL reject files larger than `MAX_UPLOAD_MB` before parsing them.
3. WHEN parsing an `.xlsx` file, THEN the system SHALL read only the first worksheet unless a
 sheet named for the importer exists, and SHALL read cell values without evaluating formulas.
4. The system SHALL match headers case-insensitively with surrounding whitespace trimmed, and
 SHALL accept columns in any order.
5. IF a required column is absent, THEN the system SHALL reject the whole file, listing every
 missing column at once, and SHALL NOT create anything.
6. The system SHALL ignore unrecognised columns and report them as informational warnings.
7. The system SHALL treat every cell as untrusted text: trimming whitespace, coercing types
 explicitly, and never rendering cell content as HTML.
8. The system SHALL skip fully blank rows silently and SHALL stop parsing at the first blank row
 only if all subsequent rows are also blank.
9. WHEN a file is rejected or an import finishes, THEN the system SHALL delete any temporary copy
 of the upload, including on the error path.
10. The system SHALL NOT store uploaded files in any web-served directory.

## Requirement 6: Rack naming convention

**User Story:** As an engineer, I want rack names to follow `datacenter-datahall-rack`
consistently, so that the device and cabling sheets can reference racks unambiguously.

### Acceptance Criteria

1. The system SHALL parse a rack name into exactly three segments split on
 `RACK_NAME_DELIMITER` (default `-`).
2. IF a rack name does not yield exactly three non-empty segments, THEN the system SHALL reject
 that row with an error quoting the received value and the expected pattern.
3. WHEN the hierarchy sheet supplies a rack name whose datacenter or datahall segment disagrees
 with that row's datacenter or datahall column, THEN the system SHALL reject the row and state
 both values.
4. The system SHALL compare rack names case-insensitively for lookup while preserving the
 supplied casing for any object it creates.
5. The system SHALL implement this convention in exactly one module, so that changing the
 delimiter or segment count is a single-file change.

## Requirement 7: Site and rack hierarchy import

**User Story:** As an engineer commissioning a new hall, I want to upload one sheet that creates
the location, datacenter, datahall, and all its racks, so that I can lay the foundation in one
step.

### Acceptance Criteria

Sheet columns — required: `location`, `datacenter`, `datahall`, `rack`, `rack_u_height`.
Optional: `rack_width`, `rack_status`, `rack_type`, `tenant`, `description`.

1. The system SHALL accept one row per rack, with the location, datacenter, and datahall values
 repeated across the rows that share them.
2. WHEN a row's location, datacenter, or datahall does not exist in NetBox, THEN the system SHALL
 create it, creating parents before children in the order region → site → location.
3. The system SHALL derive a URL-safe slug for created regions and sites from their name, and
 SHALL reject a row whose derived slug collides with a different existing object.
4. WHEN creating a site, THEN the system SHALL assign it to the region created or matched for
 that row; WHEN creating a datahall, THEN the system SHALL assign it to that site.
5. WHEN creating a rack, THEN the system SHALL set its site and its datahall location, and SHALL
 apply `rack_u_height` as the rack's height in units.
6. IF `rack_u_height` is not a positive integer within NetBox's permitted range, THEN the system
 SHALL reject that row.
7. The system SHALL default `rack_width` to 19 inches and `rack_status` to `active` when the
 optional columns are absent or blank, and SHALL validate supplied values against the choices
 NetBox reports.
8. IF two rows in one sheet declare the same rack name, THEN the system SHALL reject the later
 row as a duplicate and name the earlier row number.
9. WHEN a datahall is referenced by rows belonging to different datacenters, THEN the system
 SHALL treat them as distinct datahalls, since a NetBox location is scoped to its site.

## Requirement 8: Device import

**User Story:** As an engineer, I want to upload a sheet of devices with their rack positions,
so that racks are populated without clicking through NetBox device by device.

### Acceptance Criteria

Sheet columns — required: `rack`, `rack_face`, `bottom_ru`, `ru_height`, `device_name`, `model`.
Optional: `serial`, `manufacturer`, `device_role`, `status`, `asset_tag`, `tenant`.

1. WHEN a row's `rack` does not resolve to exactly one existing rack, THEN the system SHALL
 reject that row and SHALL NOT create the rack.
2. The system SHALL accept `rack_face` as `front` or `rear`, case-insensitively, and SHALL reject
 any other value.
3. The system SHALL interpret `bottom_ru` as the lowest rack unit the device occupies, and SHALL
 submit it as the device's position.
4. IF `bottom_ru` is not a positive integer, or `bottom_ru + ru_height - 1` exceeds the rack's
 height, THEN the system SHALL reject the row and state the rack's height.
5. WHEN the position range overlaps a device already mounted on the same face of that rack, or
 another row in the same sheet, THEN the system SHALL reject the row and identify the conflict.
6. WHEN `model` resolves to exactly one existing device type, THEN the system SHALL use it; IF
 `ru_height` disagrees with that device type's height, THEN the system SHALL reject the row and
 report both heights, because rack elevation depends on the device type.
7. IF `model` does not exist, THEN the system SHALL reject the row by default; WHERE
 `NETBOX_AUTOCREATE_DEVICE_TYPES` is enabled and `manufacturer` is supplied, the system SHALL
 instead create the manufacturer and device type using `ru_height`, and SHALL mark the row as
 having created a device type so the operator can review it.
8. The system SHALL apply `device_role` when supplied and otherwise
 `NETBOX_DEFAULT_DEVICE_ROLE`; IF neither resolves to an existing role, THEN the system SHALL
 reject the row, since NetBox requires a role.
9. The system SHALL default `status` to `active` and SHALL treat `serial` and `asset_tag` as
 optional free text, submitting them only when non-blank.
10. IF a device name already exists within the same scope, THEN the system SHALL skip the row as
 already present rather than creating a duplicate or renaming.
11. IF two rows declare the same device name, THEN the system SHALL reject the later row.
12. The system SHALL create devices in sheet order so that reported results align with the sheet.

## Requirement 9: Cabling import

**User Story:** As an engineer, I want to upload a cabling sheet covering device and patch-panel
terminations, so that connectivity is recorded without tracing each link by hand in the UI.

### Acceptance Criteria

Sheet columns — required: `a_end`, `a_end_kind`, `a_port`, `b_end`, `b_end_kind`, `b_port`,
`cable_type`. Optional: `a_port_side`, `b_port_side`, `cable_status`, `label`, `length`,
`length_unit`, `color`.

Transceiver recording is **deferred to iteration 2** — see decision D3.

1. The system SHALL accept `a_end_kind` and `b_end_kind` as `device` or `panel`, defaulting to
 `device` when blank, and SHALL use the value to decide how the port name is resolved.
2. WHERE an end is a `device`, the system SHALL resolve its port against that device's
 interfaces.
3. WHERE an end is a `panel`, the system SHALL resolve its port against that panel's front ports
 when `*_port_side` is `front` or blank, and its rear ports when the side is `rear`.
4. IF an end does not resolve to exactly one device, THEN the system SHALL reject the row naming
 the ambiguous or missing end.
5. IF a port name does not resolve to exactly one termination on the resolved device, THEN the
 system SHALL reject the row, and SHALL state which side and port name failed.
6. WHEN both terminations resolve, THEN the system SHALL create the cable by POSTing
 `a_terminations` and `b_terminations` as arrays of `{object_type, object_id}` to the cables
 endpoint, and SHALL NOT write to the read-only cable-terminations endpoint.
7. The system SHALL validate `cable_type` against the cable-type choices reported by NetBox and
 SHALL reject unrecognised values, listing the accepted values.
8. The system SHALL NOT read, require, or write any transceiver or optic information in this
 iteration, and SHALL NOT modify the `type` of any interface it did not create. A `transceiver`
 column, if present in an uploaded file, SHALL be treated as an unrecognised column per
 Requirement 5.6 — reported as informational and otherwise ignored.
9. IF either termination is already occupied by an existing cable, THEN the system SHALL skip
 the row as already cabled and identify the existing cable, and SHALL NOT modify or delete it.
10. IF a row terminates both ends on the same port, THEN the system SHALL reject it.
11. IF two rows in one sheet use the same termination, THEN the system SHALL reject the later row.
12. The system SHALL default `cable_status` to `connected` and SHALL validate `length_unit`
 against NetBox's choices when `length` is supplied.

## Requirement 10: Validation gate — nothing is written until validation is clean

**User Story:** As an engineer, I want the whole file validated before anything is written, and the
apply action unavailable until it passes, so that a bad sheet can never leave NetBox half-populated.

This is a **hard gate**, not a warning: an all-or-nothing decision per file.

### Acceptance Criteria

1. WHEN a file is uploaded, THEN the system SHALL run validation, and SHALL NOT create, update, or
 delete anything in NetBox during validation.
2. The system SHALL validate in five ordered stages: (S1) file structure, (S2) cell format and
 choices, (S3) cross-row consistency within the file, (S4) cross-reference against NetBox, and
 (S5) plan feasibility.
3. The system SHALL report every issue found within a stage rather than stopping at the first, so
 that one round of fixes can address all of them.
4. IF a stage's failure makes later stages meaningless — a missing required column, for example —
 THEN the system SHALL stop after that stage and say which stages were not reached, rather than
 emitting misleading downstream errors.
5. The system SHALL classify every issue as ERROR, WARNING, or INFO, and SHALL treat only ERROR as
 blocking.
6. IF validation produces one or more ERROR issues, THEN the system SHALL refuse to apply **any**
 part of the file, SHALL disable the apply control, and SHALL reject a direct POST to the apply
 endpoint with an explanation rather than applying anything.
7. The system SHALL NOT offer partial application of the valid rows while any ERROR remains, even
 at the operator's request.
8. WHEN validation produces zero ERROR issues, THEN the system SHALL mark the plan applyable and
 enable the apply control.
9. WHERE WARNING issues exist, the system SHALL require the operator to acknowledge them explicitly
 before enabling the apply control, and SHALL record that acknowledgement with the run.
10. The system SHALL display the verdict prominently: either blocked with the ERROR count, or ready
 with the counts of creates, skips, and acknowledged warnings.
11. The system SHALL require an explicit apply action, distinct from both the upload and the
 validate actions.
12. WHEN a plan is applied, THEN the system SHALL apply only the stored validated plan, so that a
 changed file cannot be substituted between validation and apply.
13. The system SHALL bind each plan to a content fingerprint of the uploaded file, and IF a
 re-uploaded file's fingerprint differs, THEN the system SHALL discard the previous verdict and
 require re-validation.
14. Immediately before the first write, the system SHALL re-run stage S4 against NetBox; IF any
 referenced object has appeared, changed, or disappeared since validation, THEN the system SHALL
 abort **before writing anything**, report what changed, and require re-validation.
15. The system SHALL expire a validated plan after a configurable interval, default 30 minutes, and
 require re-validation before applying.
16. WHEN a plan has already been applied, THEN the system SHALL refuse to apply it again and SHALL
 direct the user to its report.
17. The apply action SHALL be POST-only and CSRF-protected.
18. The system SHALL render the plan as a per-row table showing the spreadsheet line number, the
 intended outcome (create, skip-already-exists, or blocked), the resolved NetBox objects, and any
 issues attached to that row.

## Requirement 11: Idempotent creation

**User Story:** As an engineer, I want to re-upload a sheet after fixing a few rows, so that I
can finish a partial import without creating duplicates.

### Acceptance Criteria

1. Before creating any object, the system SHALL look it up by natural key — slug for regions and
 sites, name within parent scope for locations, racks, and devices, and existing termination
 occupancy for cables.
2. WHEN a matching object exists, THEN the system SHALL skip it, record it as already present,
 and continue with the row's remaining work.
3. The system SHALL NOT update, rename, re-parent, or delete any existing NetBox object in this
 iteration, without exception, even when sheet values differ from what NetBox holds. Every write
 this iteration performs is the creation of a new object.
4. WHEN an object already exists in NetBox and every value the sheet supplies for it matches, THEN
 the system SHALL classify the row as INFO / skip-already-exists, which does not block apply.
5. IF an object already exists but a value the sheet supplies differs from what NetBox holds, THEN
 the system SHALL raise a blocking ERROR naming the field, the sheet value, and the NetBox value,
 because iteration 1 cannot reconcile the difference by updating (see decision D8).
6. IF a globally unique field — a device name in scope, or an asset tag — is already held by a
 *different* object, THEN the system SHALL raise a blocking ERROR identifying the holder.
7. WHEN the same sheet is uploaded twice with no NetBox changes in between, THEN the second
 validation SHALL find zero ERROR issues, report every row as already present, and applying it
 SHALL create nothing.

## Requirement 12: Result reporting and partial failure

**User Story:** As an engineer, I want a precise report of what happened, so that I can fix the
sheet or unwind a partial import without guessing.

### Acceptance Criteria

1. WHEN an apply finishes, THEN the system SHALL show per-row outcomes with row numbers matching
 the uploaded file, including the created object's name and a link to it in NetBox.
2. WHEN NetBox rejects a create, THEN the system SHALL surface NetBox's own field-level error
 text against the offending row and column.
3. The system SHALL continue processing subsequent rows after a row-level failure, and SHALL abort
 the run only on connection loss or authentication failure.
4. IF a run aborts partway, THEN the system SHALL report which rows were applied before the abort,
 in order, so the operator can unwind manually.
5. The system SHALL state plainly that it cannot roll back applied creates, rather than implying
 the batch was atomic.
6. The system SHALL offer the result report as a CSV download.
7. Error messages SHALL never include the API token, request headers, or a stack trace.

## Requirement 13: Import run audit log

**User Story:** As the tool owner, I want a durable record of every import, so that a wrong bulk
change can be traced to who ran it and what it touched.

### Acceptance Criteria

1. WHEN an apply runs, THEN the system SHALL persist a run record with the user, timestamps,
 importer type, uploaded filename, target NetBox host, NetBox version, and outcome counts.
2. The system SHALL persist, for each row that resulted in a create, the NetBox object type, its
 returned id, and its name.
3. The system SHALL NOT persist the API token, the uploaded file itself, or any credential.
4. The system SHALL expose a read-only history view listing past runs, most recent first.
5. The system SHALL NOT allow run records to be edited or deleted through the web UI.

## Requirement 14: Security and data handling

### Acceptance Criteria

1. The system SHALL keep the NetBox token out of source, templates, logs, error pages, audit
 records, and commit history, and SHALL load it only from the environment.
2. The system SHALL run with `DEBUG` false and an explicit `ALLOWED_HOSTS` outside local
 development.
3. All state-changing requests SHALL require a valid CSRF token and use POST.
4. The system SHALL render all sheet-derived and NetBox-derived values through Django's
 autoescaping template layer, and SHALL NOT build HTML from those values.
5. The system SHALL communicate with NetBox over HTTPS with certificate verification enabled,
 except for the loopback development case of Requirement 2.3a, which SHALL remain impossible to
 apply to a remote host.
6. Test fixtures SHALL contain only synthetic data — no production site names, serials, customer
 identifiers, or real credentials.
7. The system SHALL log security-relevant events — login success and failure, and every apply —
 with actor and timestamp.

## Requirement 15: Scale and performance limits

### Acceptance Criteria

1. The system SHALL handle sheets of at least 2,000 rows without exhausting memory, streaming
 rows rather than materialising the whole file where the format allows.
2. The system SHALL batch or cache NetBox lookups so that validating a sheet does not issue one
 request per row per referenced object.
3. WHEN an apply exceeds the web request timeout for large sheets, THEN the system SHALL bound
 the work per request by a documented maximum row count and SHALL tell the operator to split
 the file, rather than failing silently mid-run.
4. The system SHALL display progress or a busy state while an apply is running so the operator
 does not double-submit.

## Requirement 16: Validation report and issue localisation

**User Story:** As an engineer, I want each problem pinned to a file, sheet, and line with a plain
explanation, so that I can fix the spreadsheet without hunting for what the tool meant.

### Acceptance Criteria

1. Every issue SHALL identify all of: file name, worksheet name, line number, column header, the
 received value, and what was expected.
2. Line numbers SHALL match the spreadsheet's own numbering as the operator sees it in Excel, with
 the header row as line 1, so that a reported line can be navigated to directly.
3. WHERE the upload is a `.csv`, the worksheet SHALL be reported as the file name so the format of
 an issue is identical across csv and xlsx.
4. Each issue SHALL carry a stable machine-readable code — for example `RACK_NAME_MALFORMED`,
 `RU_RANGE_OVERLAP`, `DEVICE_TYPE_NOT_FOUND`, `PORT_ALREADY_CABLED` — so that issues can be
 grouped, counted, and asserted in tests.
5. Issue messages SHALL be written for an engineer fixing a sheet, quoting the received value and
 the expectation, and SHALL NOT contain stack traces, request headers, or credentials.
6. WHEN an issue concerns two rows, such as a duplicate, THEN it SHALL name both line numbers.
7. WHEN an issue concerns an existing NetBox object, THEN it SHALL name that object and link to it.
8. The system SHALL group issues by severity and then by line, and SHALL show a count per issue code
 so that one systemic mistake reads as one problem rather than hundreds.
9. The system SHALL offer the validation report as a CSV download with one row per issue and columns
 for file, sheet, line, column, severity, code, received value, expected, and message, escaping
 values that begin with `=`, `+`, `-`, or `@`.
10. The system SHALL cap the number of issues rendered in the browser at a configurable limit,
 default 500, state that the list is truncated, and include the full set in the CSV download.
11. The system SHALL persist the validation report with its run record even when nothing was
 applied, so that a blocked attempt is auditable.

## Requirement 17: Prerequisites on an empty or unprepared instance

**User Story:** As an engineer pointing the tool at a fresh NetBox, I want to be told up front which
prerequisites are missing, so that I am not left guessing why every row in my device sheet failed.

A brand-new NetBox contains no device roles, no manufacturers, no device types, and no patch panels.
Iteration 1 creates none of these, so on an unprepared instance the device and cabling importers have
nothing to resolve against. That is expected behaviour, and the tool's job is to say so clearly
rather than to appear broken.

### Acceptance Criteria

1. The system SHALL treat a missing referenced device role, manufacturer, device type, tenant, or
 panel as a blocking stage-S4 ERROR that names the missing object and states plainly that it must be
 created in NetBox before the import can proceed.
2. Each importer page SHALL show a read-only readiness summary for that importer's prerequisites
 before any upload — for devices, whether the configured default role resolves and how many device
 types exist; for cabling, whether any devices with front or rear ports exist.
3. IF `NETBOX_DEFAULT_DEVICE_ROLE` does not resolve to an existing role, THEN the system SHALL warn
 prominently on the devices page at page load, not merely at validation time.
4. The readiness summary SHALL be strictly read-only and SHALL create nothing.
5. The system SHALL NOT create device roles, tenants, or patch panels in this iteration; device types
 only under the opt-in flag of Requirement 8.7.
6. The documentation SHALL include a runbook for preparing an empty instance: creating the device
 roles in use, importing device types, and building at least one patch panel whose front-to-rear
 port mappings use the NetBox 4.5+ `rear_ports` format.
7. WHEN the hierarchy importer runs against a completely empty instance, THEN it SHALL succeed
 without prerequisites, since it creates its own parents — so a new instance is always usable for
 sheet 1 first.

## Assumptions

1. The NetBox instance is version 4.6.x (`>=4.6, <4.7`) and reachable from wherever the app runs.
 NetBox 4.5 removed `FrontPort.rear_port` / `rear_port_position` in favour of the `PortMapping`
 model, so the pre-4.5 field names must never be used; the version probe of Requirement 2.6 exists
 partly to catch an instance outside the supported range before an import starts.
2. The supplied API token has write permission for DCIM objects and read permission for choices.
3. A single operator uses the tool at a time; no concurrent-apply coordination is required.
4. Device roles and tenants referenced by sheets already exist in NetBox; iteration 1 does not
 create them. On an unprepared instance this is surfaced by Requirement 17 rather than assumed away.
5. Patch panels already exist as devices with their front and rear ports defined, typically from
 a device type template using the 4.5+ `rear_ports` mapping format. The cabling importer does not
 create panels.
6. Rack names are globally unique in practice because they embed the datacenter and datahall.

## Decisions

- **D1 — Hierarchy mapping. DECIDED:** Location → `dcim.Region`, Datacenter → `dcim.Site`,
 Datahall → `dcim.Location`. (`dcim.SiteGroup` was the alternative, for an organisational rather
 than geographic top level.)
- **D2 — Device role. DECIDED:** optional `device_role` column plus a configurable default role
 slug, rejecting the row when neither resolves. NetBox requires a role on every device and it was
 not among the requested columns.
- **D3 — Transceiver modelling. DEFERRED to iteration 2.** The column is dropped from iteration 1
 entirely. It was briefly specified as setting the terminating interface's `type`, but that
 mechanism cannot record anything in practice: device types define interface types, so every
 inherited interface arrives already typed, and Requirement 11.3 forbids overwriting it. The column
 would have been a validation cross-check masquerading as a record of the installed optic.
 Iteration 2 should choose between a `dcim.InventoryItem` bound to the interface or a module in a
 module bay — both can actually hold a part number — rather than reviving the interface-type route.
 Dropping it also restores the create-only rule to having no exceptions at all.
- **D4 — Autocreate device types. DECIDED:** off by default, opt-in by environment variable, since
 typos would otherwise pollute a shared NetBox with junk device types.
- **D5 — Apply concurrency. DECIDED:** apply runs inline in the request with a bounded maximum row
 count. A background worker is deferred to iteration 2.

- **D5 (revised) — Apply concurrency. DECIDED:** apply still runs inline, but the validation gate of
 Requirement 10 means a run only starts once the file is known clean, so the bounded row count is
 about request duration rather than correctness.

### Still needs input

- **D6 — Transceiver map. CLOSED, moved to iteration 2** along with D3. No part-number map is needed
 while the column does not exist. When iteration 2 revisits this, the map should be built from the
 transceiver models actually in use rather than invented.
- **D7 — Bulk-create atomicity.** NetBox documents bulk *update* and bulk *delete* as all-or-none,
 but makes no such statement about bulk *create*. If a single POST of a list of objects to one
 endpoint is atomic, the half-complete window shrinks to the boundaries between endpoints, which is
 a materially stronger guarantee than row-by-row creates. This must be **verified empirically
 against a lab instance** — POST a list where one entry is invalid, then check whether the valid
 entries were created — before the apply strategy is fixed. Task 11a covers the experiment.
- **D8 — Severity of an "exists but differs" conflict.** Requirement 11.5 currently treats it as a
 blocking ERROR, on the reading that "no conflicts" means exactly that. The looser alternative is a
 WARNING the operator acknowledges, on the grounds that nothing would be written for that object
 anyway so it cannot cause a half-complete state. Blocking is stricter and noisier; confirm which
 you want.
