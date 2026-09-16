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
| Datacenter | `dcim.Site` | The physical facility. Its slug is the rack name's first segment |
| Datahall / room | `dcim.Location` | A child location scoped to the site. Its slug is the rack name's second segment |
| Rack | `dcim.Rack` | Assigned to both the site and the datahall location |
| Model | `dcim.DeviceType` | Matched by model name (and manufacturer when creating) |
| Panel | `dcim.Device` with front/rear ports | A patch panel is an ordinary device in NetBox |
| Management interface | `dcim.Interface` with `mgmt_only=true` | Named on the device type, never in the sheet. Arista `Management1`, Juniper `fxp0`/`em0`/`me0`, Cumulus `eth0` are all the same role |
| Loopback interface | `dcim.Interface` with type `loopback` | Likewise named on the device type. Arista `Loopback0`, Juniper `lo0`, Cumulus `lo` are all the same role |

**Naming convention.** A rack name is `<datacenter_slug>-<datahall_slug>-<rack>`, for example
`SYD1-DH1-R01` where the slugs are `syd1` and `dh1`. This is the join key used by all three sheets.

The first two segments are the *slugs* of the datacenter and the datahall, not their names, so that
display names can stay human — "Sydney One", "Data Hall 1" — while the rack name stays short. The
sheet supplies those slugs in their own columns, falling back to a slug derived from the name
(Requirement 7.10). Because the name is split on `RACK_NAME_DELIMITER`, neither slug may contain that
character (Requirement 6.6).

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
 present enumerated fields (rack face, cable type, cable profile, panel/device kind) as picklists.
5. WHEN the example row is left in place and uploaded unchanged, THEN the system SHALL flag it as
 example data and refuse to import it rather than creating objects named after the examples.
6. Template downloads SHALL contain no real site, device, serial, or customer values.
7. The per-column guidance SHALL occupy line 2 of the template and SHALL carry a marker in its
 first cell that the parser recognises, so that guidance can be left in place without being read as
 a data row (see Requirement 5.11).

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
8. The system SHALL skip fully blank rows silently wherever they occur, so that a blank row in the
 middle of a sheet does not truncate the import and trailing blank rows do not appear in the report.
9. WHEN a file is rejected or an import finishes, THEN the system SHALL delete any temporary copy
 of the upload, including on the error path.
10. The system SHALL NOT store uploaded files in any web-served directory.
11. The system SHALL skip the template's per-column guidance row — line 2 of a generated template,
 identified by its leading marker — without treating it as data, so that a template filled in below
 the guidance can be uploaded as downloaded.

## Requirement 6: Rack naming convention

**User Story:** As an engineer, I want rack names to follow `datacenter_slug-datahall_slug-rack`
consistently, so that the device and cabling sheets can reference racks unambiguously and a rack name
stays short even where the datacenter and datahall have long display names.

### Acceptance Criteria

1. The system SHALL parse a rack name into exactly three segments split on
 `RACK_NAME_DELIMITER` (default `-`).
2. IF a rack name does not yield exactly three non-empty segments, THEN the system SHALL reject
 that row with an error quoting the received value and the expected pattern.
3. WHEN the hierarchy sheet supplies a rack name whose first or second segment disagrees with that
 row's effective datacenter or datahall slug (Requirement 7.10), THEN the system SHALL reject the row
 and state both values.
4. The system SHALL compare rack names case-insensitively for lookup while preserving the
 supplied casing for any object it creates. A rack may therefore be named `SYD1-DH1-R01` where the
 slugs are `syd1` and `dh1`, and a sheet that predates the slug columns keeps working wherever the
 datacenter and datahall names are single tokens.
5. The system SHALL implement this convention in exactly one module, so that changing the
 delimiter or segment count is a single-file change.
6. The system SHALL reject any datacenter or datahall slug containing `RACK_NAME_DELIMITER`, whether
 that slug was supplied or derived, because the rack name is split on that character and an embedded
 delimiter would make the segment boundaries ambiguous — `syd-1` and `dh1` would produce a
 four-segment name. NetBox's own slug charset permits the hyphen, so this restriction belongs to the
 tool, and the error SHALL name the forbidden character and offer `_` as the alternative.
7. The system SHALL express 6.6 in terms of `RACK_NAME_DELIMITER` and not a literal hyphen, so that
 configuring `_` as the delimiter forbids `_` inside slugs and permits `-`, which is what keeps 6.5
 true.

## Requirement 7: Site and rack hierarchy import

**User Story:** As an engineer commissioning a new hall, I want to upload one sheet that creates
the location, datacenter, datahall, and all its racks, so that I can lay the foundation in one
step.

### Acceptance Criteria

Sheet columns — required: `location`, `datacenter`, `datahall`, `rack`, `rack_u_height`.
Optional: `datacenter_slug`, `datahall_slug`, `rack_width`, `rack_status`, `rack_form_factor`,
`tenant`, `description`.

`datacenter_slug` and `datahall_slug` are optional rather than required so that a sheet written
before they existed still validates: a blank cell falls back to a slug derived from the name, which
for a single-token name such as `SYD1` reproduces exactly what the tool derived before. They become
necessary as soon as a name is multi-word, because the derived slug would then contain the rack-name
delimiter (Requirement 6.6). The region has no slug column: its slug is never part of a rack name, so
nothing the operator can see depends on it.

The column is `rack_form_factor`, not `rack_type`: NetBox 4.2 renamed `Rack.type` to
`Rack.form_factor` and gave `rack_type` to a foreign key pointing at the separate `RackType` model.
A column named `rack_type` carrying `4-post-cabinet` would therefore be aimed at the wrong field.

1. The system SHALL accept one row per rack, with the location, datacenter, and datahall values
 repeated across the rows that share them.
2. WHEN a row's location, datacenter, or datahall does not exist in NetBox, THEN the system SHALL
 create it, creating parents before children in the order region → site → location.
3. The system SHALL set a URL-safe slug on every region, site, and datahall location it creates,
 since NetBox requires one on all three: taken verbatim from `datacenter_slug` or `datahall_slug`
 where that column is non-blank, and derived from the object's name otherwise. It SHALL reject a row
 whose slug, supplied or derived, collides with a *differently named* existing object.
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
10. The system SHALL define a level's **effective slug** as the supplied slug where its column is
 non-blank and the slug derived from its name otherwise, and SHALL use that single value for the
 NetBox lookup, for the created object's `slug`, and for the rack-name agreement check of
 Requirement 6.3 — so that those three readings of "the slug" cannot disagree with one another.
11. IF a supplied slug falls outside NetBox's slug charset of letters, digits, hyphen and underscore,
 THEN the system SHALL reject the row against the slug column. IF an effective slug contains
 `RACK_NAME_DELIMITER`, THEN the system SHALL reject the row, locating the issue on the **slug**
 column where the slug was supplied and on the **name** column where it was derived — in the latter
 case the operator's fix is to fill the slug column in, so that is where the error must point.
12. IF two rows give one datacenter or datahall name two different effective slugs, THEN the system
 SHALL reject the later row and name the earlier row's number, since one object cannot hold two
 slugs. IF two rows give two different names one effective slug, THEN the system SHALL reject the
 later row as a collision, since NetBox would treat them as a single object. Datahall slugs SHALL be
 compared within a datacenter and not globally, following 7.9.
13. IF a site or datahall already exists in NetBox and the sheet **supplied** a slug differing from
 the one it holds, THEN the system SHALL raise a blocking ERROR naming both values, because iteration
 1 never updates an existing object (Requirement 11.3) and the row asks for something the tool cannot
 deliver. A region can never reach this case, having no slug column.
14. WHERE the slug column is blank or absent and a **derived** slug differs from what a matched object
 holds, the system SHALL match on name and proceed, since a derived slug is the tool's guess rather
 than the operator's assertion and an object created outside this tool has no reason to match it. This
 is what keeps a region — and any sheet predating the slug columns — behaving exactly as before.

## Requirement 8: Device import

**User Story:** As an engineer, I want to upload a sheet of devices with their rack positions and
their management and loopback addresses, so that racks are populated without clicking through NetBox
device by device.

### Acceptance Criteria

Sheet columns — required: `rack`, `rack_face`, `bottom_ru`, `ru_height`, `device_name`, `model`.
Optional: `serial`, `manufacturer`, `device_role`, `status`, `asset_tag`, `tenant`,
`loopback_ip4`, `loopback_ip6`, `management_ip4`, `management_ip6`.

The four address columns are optional as *columns* so a sheet written before they existed still
parses, and so a patch panel — whose device type has neither interface — can leave all four cells
blank. They are not optional as *values* on a device whose type defines those interfaces: at least
one of the four cells must be filled, which is decision **D11**. Dual-stack is first-class: each
role has an IPv4 column and an IPv6 column, and a row may fill any non-empty subset. Interface
*names* are not columns. Arista, Juniper and Nvidia Cumulus all call the management port something
different, and the same is true of the loopback; putting those names in the sheet would copy the
vendor problem onto every row. They belong on the device type, which is where NetBox already keeps
them.

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
10. IF a device name already exists within the same scope, THEN the system SHALL skip creating the
 device rather than creating a duplicate or renaming, and SHALL still process that row's remaining
 work — the loopback and management addresses of 8.13–8.26 — against the existing device. A row whose
 device *and* addresses already match is reported as already present in full.
11. IF two rows declare the same device name, THEN the system SHALL reject the later row.
12. The system SHALL create devices in sheet order so that reported results align with the sheet.

Criteria 8.13–8.26 are decision **D11** — dual-stack loopback and management addresses, and the
primary IP.

13. The system SHALL accept optional `loopback_ip4`, `loopback_ip6`, `management_ip4`, and
 `management_ip6` columns. A non-blank cell SHALL be a CIDR host address with an explicit prefix
 length (`10.0.0.1/32`, `2001:db8::1/128`); a host with no prefix is rejected rather than defaulted
 to `/32` or `/128`, because a silent mask is how two engineers end up with different addresses from
 the same cell. An `*_ip4` cell SHALL be IPv4 and an `*_ip6` cell SHALL be IPv6; a family that
 disagrees with its column is rejected against that column, not silently swapped.
14. WHEN the resolved device type (or, for an existing device, the device itself) has at least one
 management or loopback interface as defined in 8.16, THEN the system SHALL require at least one of
 the four address cells to be non-blank, and SHALL reject a row that fills none. WHEN the type has
 neither interface, THEN all four cells SHALL be blank; a non-blank cell with nowhere to assign it
 is an ERROR naming the type. This is what lets a patch panel share the sheet with a switch. An
 autocreated device type (8.7) has no interface templates, so it is treated as a type with neither
 interface: all four IP cells must be blank, and the device is created with no primary IP.
15. The system SHALL NOT read, require, or write an interface *name* from the sheet. Vendor names —
 Arista `Management1` / `Loopback0`, Juniper `fxp0` / `lo0`, Cumulus `eth0` / `lo` — live on the
 device type, which is the template the device inherits from. A column of names would duplicate that
 template onto every row and would be wrong the moment a new platform used a fourth name.
16. The system SHALL resolve the management interface as the unique interface (template, on a device
 yet to be created; the device's own interface, once it exists) with `mgmt_only` set, and the
 loopback interface as the unique interface whose type is `loopback`. IF a non-blank IP cell has
 zero matching interfaces, THEN the system SHALL reject the row naming the type and the role, and
 SHALL say that the interface must be defined on the device type before the import can proceed. IF
 a non-blank IP cell has more than one matching interface, THEN the system SHALL reject the row as
 ambiguous and name them, rather than pick. Both families of one role assign to that same
 interface — a dual-stack loopback is two addresses on one `loopback` interface, not two
 interfaces.
17. WHEN an address is supplied, THEN the system SHALL create an `ipam.IPAddress` assigned to the
 resolved interface (`assigned_object_type=dcim.interface`, `assigned_object_id` of that interface,
 `status=active`) and SHALL NOT create prefixes, VRFs, or VLANs. A covering prefix is not required;
 NetBox accepts an address without one. A row may create up to four addresses.
18. The system SHALL choose the device's primary IP by family first, then by role: IPv4 is preferred
 over IPv6 unless no IPv4 address is supplied on that row; within a family, the management address
 wins over the loopback. Concretely: `primary_ip4` is `management_ip4` if that cell is non-blank,
 otherwise `loopback_ip4` if that cell is non-blank, otherwise unset; `primary_ip6` is
 `management_ip6` if that cell is non-blank, otherwise `loopback_ip6` if that cell is non-blank,
 otherwise unset. Every supplied address is still assigned to its interface. NetBox's own
 `primary_ip` (the address shown on the device) is `primary_ip4` when it is set and `primary_ip6`
 only when it is not — so a row that supplies both families is reached at its IPv4, and a
 v6-only row is reached at its IPv6. `oob_ip` is not written — see D11.
19. NetBox cannot accept a primary IP at device-create time because the address does not exist yet.
 After the address creates of 8.17 return, the system MAY PATCH the device with only `primary_ip4`
 and/or `primary_ip6` pointing at the IPs selected by 8.18, and SHALL send no other field on that
 PATCH. This is the sole exception to Requirement 11.3 in this iteration. The system SHALL NOT PATCH
 a primary IP that is already set to a different address — that is the consequential conflict of
 11.5.
20. Applying a device row SHALL be ordered: create the device (or skip it per 8.10), then create the
 addresses against its interfaces, then PATCH the primary IP fields. A re-upload whose device
 already exists therefore still creates missing addresses and still sets an unset primary, which is
 what makes a sheet that gained these columns finish the devices it created last week.
21. IF an address already exists in NetBox assigned to the intended interface, THEN the system SHALL
 skip creating it. IF it exists assigned to a different object, or unassigned, THEN the system SHALL
 raise a blocking ERROR identifying the holder, matching the asset-tag collision of 11.6: the create
 would fail or, worse, steal an address.
22. IF two cells in one sheet declare the same address — two rows, or two columns of one row —
 THEN the system SHALL reject the later cell and name the earlier location.
23. IF an existing device is missing the interface its type now defines — NetBox does not copy
 templates onto devices that already exist — THEN the system SHALL create that interface from the
 template, by the same rule and from the same source as 9.25, and assign the address to it. **Amended
 by D12.** This criterion previously required the row to be rejected, on the reasoning that adding an
 interface to a device the tool did not just create is an update of that device's inventory and so
 forbidden by 11.3. D12 rejects that reasoning: an interface is its own object, and creating one is a
 create, not a modification of the device (11.3). IF the device type defines no such template, THEN
 the row SHALL still be rejected, because the device sheet carries no interface-name or type column
 and there is nothing to create the interface *from* — that is 9.26's blank-cell branch with no cell
 to fill. **Open at review:** whether this create should sit behind `NETBOX_AUTOCREATE_INTERFACES`
 (9.24) as the cabling sheet's does. The argument for one flag is consistency; the argument against is
 that the device sheet cannot invent a name at all, so it has no typo path to guard against.
24. The system SHALL compare addresses as structured IP values, not as strings, so
 `2001:db8:0:0::1/128` and `2001:db8::1/128` are the same address (Requirement 11.5b).
25. `loopback_ip4`, `loopback_ip6`, `management_ip4`, and `management_ip6` are **consequential**
 (Requirement 11.5, decision D8): an existing device whose assigned address or primary IP disagrees
 with the sheet blocks, because later automation reaching the device by the sheet's address would
 reach the wrong place. They are not descriptive, and they are not identity — a different address
 is not a different device.
26. The plan preview SHALL name, for each supplied address, the resolved interface (so `eth0`
 versus `Management1` is visible before apply), the address that will be assigned to it, and which
 address will be `primary_ip4`, which will be `primary_ip6`, and which of those NetBox will show as
 the device's primary.

## Requirement 9: Cabling import

**User Story:** As an engineer, I want to upload a cabling sheet covering device and patch-panel
terminations, so that connectivity is recorded without tracing each link by hand in the UI.

### Acceptance Criteria

Sheet columns — required: `a_end`, `a_port`, `b_end`, `b_port`, `cable_type`. Optional:
`a_end_kind`, `b_end_kind`, `a_port_side`, `b_port_side`, `cable_profile`, `cable_group`,
`a_interface_type`, `b_interface_type`, `cable_status`, `label`, `length`, `length_unit`, `color`.

`a_end_kind` and `b_end_kind` are optional because 9.1 gives them a default. An earlier draft listed
them as required, which contradicted that default: a required column's blank cell is rejected before
any default can apply, so the default would have been unreachable. They are optional with a
documented default, like every other column that has one.

Transceiver recording is **deferred to iteration 2** — see decision D3.

`cable_profile` and `cable_group` are the breakout pair, added by decision **D10**. Criteria 9.1–9.12
describe one row as one cable with one termination on each side, and that shape cannot express a
breakout at all: a fanout from one 400G port to four 100G ports is physically *one* cable, and since a
termination carries at most one cable, four rows naming the same A port would be one create and three
rejections under 9.11. `cable_profile` records the internal channel structure NetBox needs for
per-lane path tracing; `cable_group` is what lets several rows describe the several connectors of a
single cable. Both are optional, and a sheet that uses neither behaves exactly as it does today.

`a_interface_type` and `b_interface_type` are decision **D12** — creating a device interface the sheet
names and NetBox does not have. Criteria 9.1–9.23 assume every port already exists, which is true of a
device the tool created from a prepared device type and false in the two cases operators actually hit:
a device created before its type defined the port, and a port the type does not define at all. Both
appear as `PORT_NOT_FOUND` today, and both are fixed in NetBox by hand before the sheet can be used.
The two columns supply the one fact a create needs and a cabling sheet does not otherwise carry.

1. The system SHALL accept `a_end_kind` and `b_end_kind` as `device` or `panel`, defaulting to
 `device` when blank, and SHALL use the value to decide how the port name is resolved.
2. WHERE an end is a `device`, the system SHALL resolve its port against that device's
 interfaces.
3. WHERE an end is a `panel`, the system SHALL resolve its port against that panel's front ports
 when `*_port_side` is `front` or blank, and its rear ports when the side is `rear`.
4. IF an end does not resolve to exactly one device, THEN the system SHALL reject the row naming
 the ambiguous or missing end.
5. IF a port name does not resolve to exactly one termination on the resolved device, THEN the
 system SHALL reject the row, and SHALL state which side and port name failed. This is the rule
 wherever 9.25 and 9.26 do not apply — a name matching more than one termination is always a
 rejection, and so is any unresolved name while D12's flag is off or the end is a panel (9.24, 9.28).
6. WHEN both terminations resolve, THEN the system SHALL create the cable by POSTing
 `a_terminations` and `b_terminations` as arrays of `{object_type, object_id}` to the cables
 endpoint, and SHALL NOT write to the read-only cable-terminations endpoint. The arrays hold one
 entry each for an ungrouped row; what a grouped row contributes is 9.16.
7. The system SHALL validate `cable_type` against the cable-type choices reported by NetBox and
 SHALL reject unrecognised values, listing the accepted values.
8. The system SHALL NOT read, require, or write any transceiver or optic information in this
 iteration, and SHALL NOT modify the `type` of any interface it did not create. A `transceiver`
 column, if present in an uploaded file, SHALL be treated as an unrecognised column per
 Requirement 5.6 — reported as informational and otherwise ignored. The qualifier "it did not
 create" becomes load-bearing under D12: an interface this run creates is typed once, at creation,
 from the source 9.25 or 9.26 names, and is never re-typed afterwards.
9. IF either termination is already occupied by an existing cable, THEN the system SHALL skip
 the row as already cabled and identify the existing cable, and SHALL NOT modify or delete it. This
 is the rule for a row that is its own cable; a group is judged as a whole by 9.22.
10. IF a row terminates both ends on the same port, THEN the system SHALL reject it.
11. IF two rows in one sheet use the same termination, THEN the system SHALL reject the later row,
 unless both rows belong to one cable group, where the repetition is how a shared connector is
 written (9.17).
12. The system SHALL default `cable_status` to `connected` and SHALL validate `length_unit`
 against NetBox's choices when `length` is supplied.

Criteria 9.13–9.23 are decision **D10** — cable profiles and the multi-row cables that breakout needs.

13. The system SHALL accept an optional `cable_profile` column and SHALL validate it against the
 cable `profile` choices reported by NetBox, rejecting an unrecognised value and listing the accepted
 ones. WHEN the cell is blank, the system SHALL omit `profile` from the payload entirely, so a sheet
 that does not use profiles produces byte-for-byte the cable it produces today.
14. IF the target instance reports no `profile` choices for a cable — the field renamed or withdrawn
 by some later version — THEN the system SHALL reject any non-blank `cable_profile` cell as a
 blocking ERROR naming the instance version, rather than dropping the value and creating a cable the
 operator did not describe.
15. The system SHALL accept an optional `cable_group` column and SHALL treat all rows carrying the
 same non-blank value — trimmed, matched case-insensitively — as one cable. WHEN the cell is blank,
 the row SHALL be a cable of its own, which is every sheet written before this column existed.
16. For a group, the system SHALL send as `a_terminations` the distinct A-side terminations of its
 rows in order of first appearance, and likewise for `b_terminations`, so that a fanout is written as
 one cable holding one A termination and several B terminations.
17. WHERE two rows of one group name the same termination, the system SHALL read the repetition as a
 connector shared by those rows rather than as the duplicate of 9.11. The prohibition in 9.10 —
 both ends of a single row on one port — SHALL continue to apply inside a group.
18. The system SHALL take a group's cable-level values — `cable_profile`, `cable_type`,
 `cable_status`, `label`, `length`, `length_unit`, `color` — from the first row of the group that
 supplies one, and SHALL allow every other row to leave them blank; IF a later row supplies a
 *different* non-blank value, THEN the system SHALL reject it naming both lines and both values,
 because a cable has one type and one profile and the tool must not choose between two candidates on
 the operator's behalf. This necessarily relaxes the required-cell rule of stage S2 for a grouped
 row: `cable_type` is a required column, so "required" here means required of the cable rather than
 typed on every one of its rows. A group that supplies a required value on *no* row is still
 rejected, on every row.
19. WHERE a `cable_profile` is supplied, the system SHALL compare the number of distinct terminations
 on each side against the number of connectors that profile declares for that side, and IF a side
 carries more terminations than connectors, THEN the system SHALL reject the group naming the
 profile, the limit, and the count. IF the profile's connector structure cannot be determined from
 what NetBox reports, THEN the system SHALL skip the comparison and raise a WARNING rather than
 invent a limit. Measured on 4.6.8: NetBox permits *fewer* terminations than connectors, so a partly
 populated breakout is legal and this check is `<=`; and NetBox enforces the upper bound itself, so
 this check exists to report at validation time rather than to be the only guard.
20. IF a group resolves to more than one distinct termination on either side and no `cable_profile`
 is supplied, THEN the system SHALL raise a WARNING for acknowledgement: NetBox will store such a
 cable, but with no profile it cannot trace which lane reaches which far end, which is the entire
 reason for recording a breakout rather than four separate links.
21. The system SHALL NOT write `connector` or `positions` on any termination — NetBox derives both
 from the profile and exposes them read-only — and SHALL therefore treat the order of a group's rows
 as its lane order. The plan preview SHALL state the connector each row's termination will receive,
 so lane assignment is read before apply rather than discovered after it. Measured on 4.6.8: four
 terminations submitted in order come back as connectors 1, 2, 3, 4, so the assignment is positional
 and the preview's numbers are the ones NetBox will use.
22. The system SHALL judge occupancy over a whole group: WHERE every termination is free it SHALL
 create the cable; WHERE every termination is already held by one and the same cable it SHALL skip
 the group as already cabled and name that cable; and IF the terminations are split — some free, or
 held by more than one cable — THEN the system SHALL reject the group, since iteration 1 cannot add a
 termination to a cable that already exists (11.3) and a half-cabled breakout needs a human decision.
23. WHEN a group's cable is created, THEN the system SHALL record it against every line of the group
 in the run report, so each line the operator wrote has a result of its own.

Criteria 9.24–9.33 are decision **D12** — creating a missing device interface rather than refusing the
row that names it.

24. The system SHALL read a flag from the environment, `NETBOX_AUTOCREATE_INTERFACES`, defaulting to
 off. WHILE it is off, 9.5 stands unaltered in every particular — an unresolved port name is
 `PORT_NOT_FOUND` and the file is blocked — and the two `*_interface_type` columns are accepted,
 validated, and otherwise unused. Every criterion from 9.25 to 9.33 applies only while the flag is on,
 except 9.27, which validates those two columns in either state so that a bad value is caught on the
 sheet that will one day be applied with the flag on. This mirrors device-type autocreate (8.7, D4)
 and for the same reason: a shared NetBox should not gain objects from a typo in a sheet nobody
 expected to create anything.
25. WHILE the flag is on, WHERE an end is a `device`, its port name matches no interface on that
 device, and the device's *device type* defines an interface template of that name, THEN the system
 SHALL plan the creation of that interface from the template — `name` and `type`, together with every
 other field the template carries that `dcim.Interface` accepts — and SHALL then terminate the cable
 on it. Nothing here is invented: the created interface is the one NetBox would have produced had the
 template existed when the device was created. This is the common case, because NetBox does not copy
 a newly added template onto devices that already exist.
26. WHILE the flag is on, WHERE the device type defines no template of that name, THEN the system
 SHALL create the interface only if the row supplies the matching `*_interface_type` cell, using the
 port name verbatim and the supplied type; IF that cell is blank, THEN the system SHALL reject the row
 with a code distinct from `PORT_NOT_FOUND`, stating that the name is unknown to the device type and
 naming both remedies — add the template to the type, or fill the column. The tool SHALL NOT default
 the type, because every default here is a guess recorded as fact about physical hardware.
27. The system SHALL validate a non-blank `a_interface_type` / `b_interface_type` against the
 interface `type` choices reported by NetBox, rejecting an unrecognised value and listing the accepted
 ones. WHERE the cell is filled on a row whose port already exists, the system SHALL NOT compare it to
 that interface's type and SHALL NOT modify it (9.8); IF the two disagree, THEN the system SHALL raise
 an INFO saying NetBox's value stands.
28. Every interface the system plans to create SHALL raise a WARNING requiring acknowledgement before
 apply (Requirement 10.9), naming the device, the interface, the type it will be given, and whether
 that type came from the device-type template or from the sheet. A mistyped port name and a genuinely
 new port are indistinguishable to the tool, so the acknowledgement is the only place `swp1o` gets
 noticed before it exists in NetBox.
29. The system SHALL NOT create a termination for an end whose `*_end_kind` is `panel`. A front port
 needs a rear port and a position mapping that no cabling column expresses, and a rear port needs a
 position count, so an unresolved panel port remains `PORT_NOT_FOUND` under this decision as under
 9.5 — see assumption 5. The system SHALL NOT create devices, of either kind, under any circumstances
 (9.4).
30. The system SHALL create each interface at most once per run however many rows or cable groups name
 it, and SHALL terminate every one of those rows on that one created interface. Whether two rows
 naming the same missing interface are the duplicate of 9.11 or the shared connector of 9.17 SHALL be
 decided exactly as it would be if the interface already existed: creation does not change which.
31. IF two rows would create the same interface on the same device with *different* non-blank
 `*_interface_type` values, THEN the system SHALL reject the later row naming both lines and both
 values, for the reason 9.18 gives about a cable's own attributes — an interface has one type and the
 tool must not choose between two candidates on the operator's behalf.
32. The system SHALL create interfaces before cables, as a bulk POST to `dcim.interfaces` preceding
 the POST to `dcim.cables`, and the cable payload SHALL reference each created interface by the id
 NetBox returned. This makes the cabling sheet two endpoints rather than one, so D7's half-complete
 window — which this sheet uniquely did not have — now exists between them: interfaces written, cables
 not. Re-uploading the sheet closes it, because the interfaces are then found rather than created and
 only the cables remain to write (Requirement 11.7). The pre-apply re-check of Requirement 10.14 SHALL
 cover the planned interfaces: IF one has appeared in NetBox since validation, THEN the run SHALL
 abort before writing anything rather than submit a create NetBox will reject as a duplicate name.
33. The plan preview SHALL distinguish a port that was found from one that will be created, naming the
 type and its source for the latter, and the run report SHALL record each created interface as an
 object of its own, so what the report counts matches what was written (Requirements 10.18, 12.1).

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
 occupancy for cables. For regions, sites, and datahalls the slug used is the effective slug of
 Requirement 7.10, so a sheet that states a slug is looked up by the slug it states.
2. WHEN a matching object exists, THEN the system SHALL skip it, record it as already present,
 and continue with the row's remaining work.
3. The system SHALL NOT update, rename, re-parent, or delete any existing NetBox object in this
 iteration, even when sheet values differ from what NetBox holds. Every write this iteration
 performs is the creation of a new object, with one exception: the primary-IP PATCH of Requirement
 8.19, which sends only `primary_ip4` / `primary_ip6` and only to set them to an address this run
 assigned (or found already assigned) on that device. A primary IP already set to a *different*
 address is not patched — it is the consequential conflict of 11.5.
3a. Creating a child object against a device that already exists — an `ipam.IPAddress` under 8.17, a
 `dcim.Interface` under 8.23 or 9.25 — is a create and not an update, so 11.3 permits it. What 11.3
 forbids is changing a field of an object this run did not write, and none of those paths does: no
 existing interface is re-typed or renamed (9.8), and no existing cable gains a termination (9.22).
 The distinction is what keeps the invariant checkable — `PATCH`, `PUT`, and `DELETE` appear in
 exactly one place in the codebase, the primary-IP call of 8.19.
4. WHEN an object already exists in NetBox and every value the sheet supplies for it matches, THEN
 the system SHALL classify the row as INFO / skip-already-exists, which does not block apply.
5. IF an object already exists but a **consequential** value the sheet supplies differs from what
 NetBox holds, THEN the system SHALL raise a blocking ERROR naming the field, the sheet value, and
 the NetBox value, because iteration 1 cannot reconcile the difference by updating, and because a
 difference in these fields means later rows are built on a false premise (decision D8).
 Consequential fields are: a region's parent; a site's region; a location's site and parent; a
 rack's `u_height` and `width`; a device's rack, position, face, and device type; a device's
 `loopback_ip4`, `loopback_ip6`, `management_ip4`, `management_ip6`, and primary IP (Requirement
 8.25); a cable's `type` and
 `profile`; and a slug the sheet supplied explicitly for a site or datahall, per Requirement 7.13 — a
 slug the tool merely derived is not consequential, per 7.14. A cable's `profile` sits on this side
 because it is what NetBox traces lanes through: an existing cable profiled differently from the sheet
 means the operator's picture of that link is wrong, not merely differently labelled.
5a. IF the differing value is **descriptive** — one that nothing else in the import depends on —
 THEN the system SHALL raise a WARNING that the operator must acknowledge before applying, rather
 than blocking. Descriptive fields are: `description`, `status`, `tenant`, `serial`, `asset_tag`,
 `rack_form_factor`, and a cable's `label`, `color`, `length`, and `length_unit`.
5b. WHEN comparing a sheet value against NetBox, the system SHALL compare structured values —
 identifiers, slugs, integers, and choice values — exactly, and SHALL compare free text after
 trimming and collapsing whitespace and folding case, so that a difference of presentation alone is
 not reported as a conflict.
5c. The classification in 11.5 and 11.5a SHALL live in one table in code, so that adding a sheet
 column forces an explicit decision about which side it falls on.
6. IF a globally unique field — a device name in scope, an asset tag, or an IP address — is already
 held by a *different* object, THEN the system SHALL raise a blocking ERROR identifying the holder.
 This outranks 11.5a: an asset tag recorded differently *on the matched object* is a descriptive
 warning, but the same tag held by *another* object is a collision and blocks, because the create
 would fail. An IP assigned to a different interface is the same class of collision (Requirement
 8.21).
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
 text against the offending row and column. IF NetBox returns a single error that is not aligned to
 a specific item in the submitted batch, THEN the system SHALL replay the batch one row at a time to
 identify the offending row before reporting.
3. WHEN NetBox rejects a batch, THEN the system SHALL treat every row in that batch as not applied
 (per D7 the batch rolled back), SHALL NOT proceed to the next endpoint, and SHALL abort the run on
 connection loss or authentication failure.
4. IF a run aborts partway, THEN the system SHALL report which endpoints completed before the abort,
 and the rows they created, so the operator can unwind manually.
5. The system SHALL state plainly what its guarantee is and is not: each endpoint's creates are
 applied all-or-nothing, the sequence across endpoints is not, and no applied create can be rolled
 back by this tool.
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
 `RU_RANGE_OVERLAP`, `DEVICE_TYPE_NOT_FOUND`, `PORT_ALREADY_CABLED`, `DEVICE_IP_REQUIRED`,
 `DEVICE_IP_FAMILY_MISMATCH`, `MGMT_INTERFACE_NOT_FOUND`, `INTERFACE_TYPE_REQUIRED` — so that issues
 can be grouped, counted, and asserted in tests.
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

1. The system SHALL treat a missing referenced device role, manufacturer, device type, tenant,
 panel, or — when a device row supplies a loopback or management address — a missing management or
 loopback interface on that type, as a blocking stage-S4 ERROR that names the missing object and
 states plainly that it must be created in NetBox before the import can proceed.
2. Each importer page SHALL show a read-only readiness summary for that importer's prerequisites
 before any upload — for devices, whether the configured default role resolves, how many device
 types exist, and how many of those types define a `mgmt_only` interface and a `loopback`-typed
 interface; for cabling, whether any devices with front or rear ports exist, and whether
 `NETBOX_AUTOCREATE_INTERFACES` is on, since that flag changes what an unknown port name does
 (9.24).
3. IF `NETBOX_DEFAULT_DEVICE_ROLE` does not resolve to an existing role, THEN the system SHALL warn
 prominently on the devices page at page load, not merely at validation time.
4. The readiness summary SHALL be strictly read-only and SHALL create nothing.
5. The system SHALL NOT create device roles, tenants, or patch panels in this iteration; device types
 only under the opt-in flag of Requirement 8.7, and device interfaces only under the opt-in flag of
 Requirement 9.24.
6. The documentation SHALL include a runbook for preparing an empty instance: creating the device
 roles in use, importing device types, marking exactly one `mgmt_only` interface and exactly one
 `loopback`-typed interface on each network device type (community templates often type a loopback
 as `virtual` — that is not this tool's loopback), and building at least one patch panel whose
 front-to-rear port mappings use the NetBox 4.5+ `rear_ports` format.
7. WHEN the hierarchy importer runs against a completely empty instance, THEN it SHALL succeed
 without prerequisites, since it creates its own parents — so a new instance is always usable for
 sheet 1 first.

## Assumptions

1. The NetBox instance is version 4.6.x (`>=4.6, <4.7`) and reachable from wherever the app runs.
 NetBox 4.5 removed `FrontPort.rear_port` / `rear_port_position` in favour of the `PortMapping`
 model, so the pre-4.5 field names must never be used; the version probe of Requirement 2.6 exists
 partly to catch an instance outside the supported range before an import starts. Cable profiles
 arrived in the same 4.5 release, so the supported range guarantees them and the `cable_profile`
 column of Requirement 9.13 needs no version condition of its own — 9.14 covers only their
 disappearance in some later version.
2. The supplied API token has write permission for DCIM objects — including `dcim.Interface`, which
 Requirements 8.23 and 9.25 create — write permission for `ipam.IPAddress` (Requirement 8.17), and
 read permission for choices. It does not need permission to create prefixes, VRFs, or VLANs.
3. A single operator uses the tool at a time; no concurrent-apply coordination is required.
4. Device roles and tenants referenced by sheets already exist in NetBox; iteration 1 does not
 create them. On an unprepared instance this is surfaced by Requirement 17 rather than assumed away.
5. Patch panels already exist as devices with their front and rear ports defined, typically from
 a device type template using the 4.5+ `rear_ports` mapping format. The cabling importer does not
 create panels, and D12 does not change that: it creates device interfaces only (9.29). A front port
 is meaningless without the rear port and position it maps to, and that mapping is a property of the
 panel's internals rather than of any link, so it stays device-type preparation.
6. Rack names are globally unique in practice because they embed the datacenter and datahall slugs.
7. NetBox field names are taken from the target instance rather than from memory. Two 4.x renames
 caught during spec verification against 4.6.8 are worth naming, because a sheet column pointed at
 the old name would fail or, worse, hit a different field: `Rack.type` became `Rack.form_factor` in
 4.2 (`rack_type` is now a foreign key to the `RackType` model), and `dcim.Location` requires a
 `slug` just as regions and sites do.
8. NetBox validates a slug against `^[-a-zA-Z0-9_]+$` with a maximum length of 100, so the hyphen is
 legal in a NetBox slug even though Requirement 6.6 forbids it in the two slugs that a rack name is
 built from. Uppercase is likewise legal, and a supplied slug is submitted with the casing the sheet
 gave it rather than being folded, matching Requirement 6.4's treatment of names.

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
 count; a background worker is deferred to iteration 2. Revised once the validation gate of
 Requirement 10 landed: a run only starts on a file already known to be clean, so the bounded row
 count is now about request duration rather than correctness.
- **D6 — Transceiver map. CLOSED, moved to iteration 2** along with D3. No part-number map is needed
 while the column does not exist. When iteration 2 revisits this, the map should be built from the
 transceiver models actually in use rather than invented.
- **D7 — Bulk-create atomicity. DECIDED: bulk create IS atomic** (verified against NetBox 4.6.8 on
 2026-08-18; see `design.md` for the experiment and its output). All three failure classes rolled
 back completely: a field-level validation error, a deferred database uniqueness violation that
 surfaced as an HTTP 500 mid-write, and a NetBox model `clean()` error. In every case nothing
 survived. Apply therefore issues **one bulk POST per endpoint**, which shrinks the half-complete
 window from "between any two rows" to "between two endpoints".
- **D8 — Severity of an "exists but differs" conflict. DECIDED: split by field.** Neither uniform
 answer was right. A difference in a *consequential* field — an existing site in a region the sheet
 disagrees with, a rack whose real `u_height` is not what the sheet assumes, a device found in a
 different rack — means the operator's picture of NetBox is wrong, and every row built on top of
 that object is suspect, so it blocks. A difference in a *descriptive* field that nothing else
 reads cannot produce a wrong result, so it warns and the operator acknowledges it. Blocking on
 everything would have made a one-word description mismatch stop a 4,000-row import, which is how a
 safety gate loses its credibility and gets switched off. See Requirements 11.5, 11.5a, 11.5b.
- **D9 — Slugs in the sheet, and in rack names. DECIDED:** the hierarchy sheet gains optional
 `datacenter_slug` and `datahall_slug` columns, and a rack name is built from those two slugs rather
 than from the datacenter and datahall names. Deriving the slug from the name was fine while the slug
 was invisible plumbing, but it forced the *name* to double as the short code: a datacenter had to be
 called `SYD1` rather than "Sydney One" purely so the rack name would come out short. The slug columns
 separate the two jobs.
 Three sub-decisions were taken with it. **Optional, with a derived fallback**, so no existing sheet
 breaks and the single-token cases keep working untouched. **A supplied slug that disagrees with an
 existing object blocks** rather than warning — the operator asserted a value that iteration 1
 cannot write, and silently using a different one would leave rack names pointing at a slug NetBox
 does not have. **A derived slug that disagrees does not block** (Requirement 7.14): applying the
 blocking rule to the fallback would make every pre-existing object whose slug this tool did not
 derive start rejecting rows that work today.
 The alternative — keep deriving, and require the *name* to be delimiter-free — was rejected because
 it fixes the rack-name problem by making the names worse, which is the constraint the change set out
 to remove. The cost of D9 is that the delimiter ban of Requirement 6.6 becomes load-bearing, and
 that a multi-word name now needs its slug column filled in.
- **D10 — Cable profiles and breakout. PROPOSED, awaiting review.** The cabling sheet gains two
 optional columns: `cable_profile`, validated against NetBox's own profile choices, and `cable_group`,
 which collects several rows into one cable with several terminations per side.
 **Why two columns and not one.** The column on its own is nearly useless for the deployments that
 asked for it. NetBox will accept a breakout profile on a cable with a single termination each side,
 so a one-column change would validate and apply — but it would still model a 1-to-4 fanout as four
 separate cables, and it could not even do that: all four share the A-side port, a termination carries
 one cable, so three of the four rows are rejected by Requirement 9.11 before any profile is read. The
 profile is the vocabulary; the grouping is what makes the vocabulary sayable.
 **Grouping is explicit, not inferred.** Rows could have been grouped implicitly by a shared A-side
 termination, saving the operator a column. Rejected on two grounds. A trunk profile has several
 connectors on *both* sides — a 2C4P trunk is two A terminations and two B terminations — so the A
 port is not a group identity in the general case. More importantly, two rows sharing an A port is an
 ERROR today, and inferring a group would silently reinterpret a mistyped duplicate as a deliberate
 breakout: exactly the class of quiet reinterpretation the validation gate exists to prevent. A blank
 `cable_group` therefore keeps today's meaning, and grouping only happens where somebody asked for it.
 **Lane order is row order, because nothing else is available.** NetBox assigns each termination its
 connector and positions from the profile and exposes both read-only, so the only lever the API offers
 is the order of the terminations array (Requirement 9.21). Two alternatives were considered and not
 taken: an explicit `a_connector` / `b_connector` pair of columns, which is the honest fix but adds two
 columns and an ordering-validation rule for a benefit no deployment has asked for yet; and doing
 nothing at all, which leaves lane assignment invisible. The middle position is that the plan preview
 names the connector each row will get, so re-sorting a sheet changes something the operator can see
 before consenting rather than after. Revisit if sheets turn out to arrive with lane numbers already in
 them.
 **Cost if reversed:** `columns.py`, the cabling importer's S3 and planning, one new grouping hook on
 `BaseImporter`, and the per-line report attribution. Nothing outside the cabling path changes.

- **D11 — Dual-stack loopback and management addresses on the device sheet. PROPOSED, awaiting
 review.** The device sheet gains four optional columns: `loopback_ip4`, `loopback_ip6`,
 `management_ip4`, `management_ip6`. At least one must be filled on any row whose device type
 defines those interfaces. Primary IP is chosen by family first, then by role: IPv4 wins over IPv6
 unless the row supplies no IPv4 at all; within a family, management wins over loopback. Interface
 names are not columns.
 **Why four columns rather than two cells that accept either family.** A single `loopback_ip` that
 might be v4 or v6 cannot say "both". Comma-separated dual values in one cell are how a sheet
 becomes un-diffable and how a missing mask on one of two addresses is reported against the wrong
 one. Four columns make family a property of the header, which is what S2 can check.
 **Why family first, then role.** "IPv4 preferred unless no IPv4 is supplied for the device" is a
 property of the row, not of a role. A row with `management_ip6` and `loopback_ip4` is therefore
 reached at the loopback IPv4, with the management IPv6 still assigned and written as
 `primary_ip6`. Role-first would have made that device v6-primary while an IPv4 sat on it, which is
 the opposite of the preference. Within the winning family, management still beats loopback, which
 is the original rule for a single-family row.
 **Why the names stay on the device type.** Arista uses `Management1` and `Loopback0`, Juniper uses
 `fxp0` (or `em0`, or `me0`) and `lo0`, Nvidia Cumulus uses `eth0` and `lo`. A sheet column of names
 would make every row restate a fact the device type already holds, and would be wrong for the next
 platform. NetBox already has the discriminators: `mgmt_only` on an interface template is the
 management port, and type `loopback` is the loopback. Both families of one role assign to that
 same interface. The operator's job is to mark those on the type once — including correcting a
 community YAML that typed `Loopback0` as `virtual` — and then type only addresses in the sheet.
 **Why at-least-one, not both-roles-required, and not both-optional.** A device reachable on one
 address is enough to be the primary IP; forcing both roles would reject a loopback-only box or a
 management-only OOB box, and forcing both families would reject a v4-only hall. Forcing none would
 let a switch import with no address at all, which is the gap this change exists to close. A type
 with neither interface (a patch panel) is the exemption, so a mixed sheet still works.
 **Why this is the first PATCH.** NetBox will not accept `primary_ip4` on the device create: the IP
 does not exist yet. The sequence is create device → create addresses on its inherited interfaces →
 PATCH only the primary-IP fields. That PATCH is scoped to those two fields and does not reopen
 update-in-general. Completing a partly-cabled breakout (D10 / 9.22) stays deferred for that reason.
 **Cost if reversed:** `columns.py`, the device importer's S2/S3/S4 and apply (a third endpoint,
 `ipam.ip-addresses`, and a PATCH), Requirement 11.3's exception, the devices readiness panel, and
 the device-type prep in the empty-instance runbook. Cabling is untouched.
 One question inside D11 remains the operator's: whether a supplied management address should also
 be written to NetBox's `oob_ip` (the sheet rule as stated puts it on `primary_ip*` and leaves
 `oob_ip` alone). Dual-stack is decided: four columns, family first.

- **D12 — Creating a missing device interface from the cabling sheet. PROPOSED, awaiting review.**
 A cabling row naming a port NetBox does not have is `PORT_NOT_FOUND` today, and the operator's only
 remedy is to create the interface by hand and re-upload. D12 lets the tool create it: from the device
 type's interface template where one of that name exists, and otherwise from two new optional columns,
 `a_interface_type` and `b_interface_type`. Off by default, behind `NETBOX_AUTOCREATE_INTERFACES`.
 **Why the template is the first source and the columns only the fallback.** The case that actually
 arrives is not an undocumented port; it is a device created before its type defined the port, because
 NetBox does not copy a newly added template onto existing devices. The template already holds the
 name, the type, `mgmt_only`, and the speed, so creating from it invents nothing and produces exactly
 the interface the device would have had. It also fails safe on the error this feature is most likely
 to cause: a mistyped `swp1o` matches no template either, so it does not quietly become a port. Making
 the columns the *only* source would have thrown that away and asked every row to restate a fact the
 device type already holds — the argument D11 makes about interface names, applied to their types.
 **Why the columns exist at all.** A type that genuinely does not define the port is a real case —
 a modular chassis, a line card added after the type was written, a device type nobody owns — and
 without the columns those sheets stay blocked with no route forward but the NetBox UI. What the tool
 must not do is *default* the type: `1000base-t` written into NetBox as a fact about a 400G cage is
 worse than the blocked row it replaced, because nothing later distinguishes it from a measured value.
 So the fallback demands an explicit value and rejects the row without one.
 **Why off by default, and why a warning even when on.** This is D4's argument (autocreate device
 types) applied to a smaller object with a larger blast radius, since a cabling sheet names two ports
 per row. The tool cannot tell a typo from a new port; only the operator can. The flag keeps a shared
 instance safe from a sheet nobody meant to be creative, and the per-interface acknowledgement (9.28)
 puts the list of names about to be created in front of the person who typed them. The cost is that
 the feature does nothing until somebody sets an environment variable, which is the same cost D4 pays.
 **Why interfaces and not panel ports.** A front port is defined by the rear port and position it maps
 to; the sheet has no column for either, and inferring a mapping would fabricate a panel's internal
 wiring. Rear ports need a position count. Both stay preparation (assumption 5).
 **What it costs elsewhere, and this is the real price.** The cabling sheet becomes two endpoints
 instead of one, so it loses the property that made it the safest of the three: a single bulk POST,
 hence no half-applied window at all (D7). Between the interface batch and the cable batch there is
 now a window where interfaces exist and cables do not. That state is benign — spare interfaces on a
 device, which a re-upload then cables — but it is no longer *nothing*, and `importers/cabling.py`
 currently documents the old property in its module docstring.
 **Cost if reversed:** two `ColumnSpec`s, the cabling importer's port resolution and a second entry in
 its `endpoint_order`, the pre-apply drift check over planned interfaces, one settings flag, and the
 amendment to Requirement 8.23. `BaseImporter` needs nothing new — `Ref` and `Registry` already carry
 an id from an earlier batch into a later payload, which is how the device sheet's addresses reference
 the device they were created against.
 Two questions inside D12 are the reviewer's. Whether the template-backed create of 9.25 should sit
 behind the flag at all, given it can invent nothing and covers the common case — the alternative is
 flag-free 9.25 with only the column-driven 9.26 gated. And whether Requirement 8.23's device-sheet
 create should share the flag or stay unconditional.

D1–D9 are settled; D3 and D6 are deferred to iteration 2 by choice, not left undecided. **D10, D11,
and D12 are the open decisions.** Inside D10: whether grouped rows must repeat the cable's attributes
or inherit them from the first row (9.18 proposes inherit), and whether lane order can be left as row
order (9.21) or needs explicit connector columns. Inside D11: `oob_ip` versus primary-only. Inside
D12: whether the flag covers the template-backed create as well as the column-driven one, and whether
Requirement 8.23 shares it.
