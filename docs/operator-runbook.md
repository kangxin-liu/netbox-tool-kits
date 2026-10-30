# Operator runbook

Classification: INTERNAL. The sheets you upload describe infrastructure and are INTERNAL; the NetBox
API token is CONFIDENTIAL and belongs only in `.env` or your secret manager.

This is the day-to-day guide: how to fill a sheet in, what the verdict is telling you, and what to do
when something goes wrong. If you are pointing the tool at a NetBox that has never been used before,
read [`empty-instance-prep.md`](empty-instance-prep.md) first — sheet 1 requires a prepared site and
parent location, and sheets 2 and 3 have additional prerequisites.

## The one thing to know

**Iteration 1 only ever creates objects**, with one named exception: when a device row explicitly
selects a primary address it PATCHes only that device's primary-IP fields. It never updates anything
else and never deletes. If an object already exists it is skipped, which is why re-uploading the
same sheet is safe and why fixing a wrong value in NetBox is a job for NetBox, not for this tool.

**A single error blocks the whole file.** Not the row — the file. There is no partial apply, because
a half-populated NetBox is the exact failure this tool exists to prevent. Until the verdict is clean
there is no apply button, and a hand-crafted POST to the apply endpoint is answered with `409` and
the reason.

## The three sheets, in order

Each page carries its own **Download .xlsx** and **Download .csv** buttons, and both templates are
generated from the running code, so a downloaded template can never be out of date. Each template
carries a guidance line explaining every column and one synthetic example row — replace the example
row, since it is refused as example data.

| Order | Page | Creates | One row is |
| --- | --- | --- | --- |
| 1 | Site & rack hierarchy | direct child locations and racks | one rack |
| 2 | Devices | devices mounted in racks, plus loopback and management addresses | one device |
| 3 | Cabling | cables between devices and panels | one cable, unless rows share a `cable_group` |

Run them in that order. Sheet 2 resolves racks created by sheet 1; sheet 3 resolves devices created
by sheet 2.

## Device loopback and management addresses

Four optional columns on the device sheet. A patch panel — whose device type has no management port
and no loopback — must leave all four blank.

**Mount first, address later.** A network device may also leave all four blank when its IP plan is
not ready. The device is created with its interfaces but no addresses and no primary IP, and each
such row raises a `DEVICE_IP_DEFERRED` warning that you acknowledge once before applying. When the
plan lands, upload the same rows again with the addresses filled: the devices already exist, so only
the addresses (and any primary you select) are created. A device that already exists and still has
blank cells is reported as already present, with no second warning.

| Column | What it does |
| --- | --- |
| `management_ip4` | IPv4 of the out-of-band / management port |
| `management_ip6` | IPv6 of the same port |
| `loopback_ip4` | IPv4 of the loopback |
| `loopback_ip6` | IPv6 of the same loopback |
| `primary address v4` | Optional address to set as `primary_ip4` |
| `primary address v6` | Optional address to set as `primary_ip6` |

Each cell takes a CIDR host address with an explicit mask (`10.0.0.1/32`, `2001:db8::1/128`). A
bare host with no mask is rejected rather than guessed. An `*_ip4` cell must be IPv4 and an `*_ip6`
cell must be IPv6.

**Which address is primary.** Selection is explicit: fill `primary address v4`, `primary address
v6`, or both. The value must exactly match a CIDR assigned to any interface on that device. It may
match one of the four addresses created by the same row, or an address already assigned to an
existing device's interface. It does not have to be a management or loopback address.

| Primary columns | Result |
| --- | --- |
| both blank | No primary-IP change |
| only `primary address v4` | Sets `primary_ip4` |
| only `primary address v6` | Sets `primary_ip6` |
| both filled | Sets both; NetBox displays IPv4 |

**Do not put the interface name in the sheet.** Arista calls the management port `Management1`,
Juniper calls it `fxp0` (or `em0`, or `me0`), Nvidia Cumulus calls it `eth0`. The loopback is
`Loopback0`, `lo0`, or `lo` for the same three. Those names live on the device type in NetBox —
one `mgmt_only` interface and one interface typed `loopback`. Both families of one role go on that
same interface. The plan preview shows the name that will be used, so you can see `eth0` versus
`Management1` before you apply.

If a switch row is rejected because the type has no such interface, that is prep, not a sheet fix:
edit the device type (community YAML often types `Loopback0` as `virtual`, which this tool will
not treat as a loopback), then re-upload. NetBox does not copy a newly added template onto devices
that already exist, so fix the type *before* the first import of that model.

A re-upload of a device that already exists still creates missing addresses and can set an unset
primary IP when the corresponding primary column is filled. An address already held by a different
device is an error, not a steal.

## Breakout cables

A breakout is **one** cable, not several. The cable terminates on physical parent interfaces; the
sheet may also create virtual child interfaces that record the lane map. Four optional columns say so:

| Column | What it does |
| --- | --- |
| `cable_group` | Any tag you like. Every row carrying the same tag is **one cable**. Blank means the row is a cable on its own, which is how every cabling sheet worked before these columns existed |
| `cable_profile` | How the cable is built inside, which is what lets NetBox trace one lane rather than treating the cable as a single hop. The template's picklist carries the values your NetBox accepts |
| `a_parent` / `b_parent` | Optional physical-parent interface name on that row's `a_end` / `b_end` device. When filled, that side's `*_port` is a virtual child created under the parent, while the cable terminates on the parent |
| `a_interface_type` / `b_interface_type` | Types a missing physical parent. It never types the child: breakout children are always `virtual` |

Parent and child names are used verbatim; no vendor naming convention is parsed. For example,
Huawei/H3C may use `a_parent=100GE1/1/1` with `a_port=100GE1/1/1:1`. The parent device is already
unambiguous from `a_end`, so it is not repeated in `a_parent`.

One row per far end, all of them carrying the same tag:

```
a_end,            a_port, a_parent, b_end,           b_port, cable_type, cable_profile,      cable_group
exa1-dh1-spine01, swp1s0, swp1,     exa1-dh1-leaf01, swp31,  mmf-om4,    breakout-1c4p-4c1p, brk-01
exa1-dh1-spine01, swp1s1, swp1,     exa1-dh1-leaf02, swp31,  ,           ,                   brk-01
exa1-dh1-spine01, swp1s2, swp1,     exa1-dh1-leaf03, swp31,  ,           ,                   brk-01
exa1-dh1-spine01, swp1s3, swp1,     exa1-dh1-leaf04, swp31,  ,           ,                   brk-01
```

Five things worth knowing before you fill one in.

**The later rows can be left blank.** A cable has one type, one profile and one label, so state them
once — on any row of the group — and leave those cells empty on the rest. That includes `cable_type`,
which is a required column but is only required *of the cable*. Two rows of one group giving different
non-blank values is an error naming both lines, because the tool will not choose between them.

**Which profile to pick.** Count the far-end connectors, not the fibres. A 400G-SR8 splitting into two
200G-SR4 links has eight lanes but only two far ends, so it is `breakout-1c2p-2c1p`. A profile may be
only partly filled: two lanes of a four-lane breakout is a real cable. Naming *more* far ends than the
profile has connectors is an error that tells you the limit.

**The profile also tags each physical parent.** Before using parented breakout rows, NetBox must
contain tag slugs `port-breakout-2x`, `port-breakout-4x`, and `port-breakout-8x`. The importer uses
the number of positions on that profile side per connector: 2, 4, or 8 positions adds the matching
tag; one position adds none. Row count does not change the factor, so one connected child of a 4x
breakout still tags the parent `port-breakout-4x`. A 2C4P shuffle tags each A-side parent 4x, not 8x.
Profiles with 6, 12, or 16 positions block because this deployment has no approved matching tag.

Existing unrelated parent tags are preserved. If the correct breakout tag is already present, no
update is sent. A different `port-breakout-*` factor blocks the file; correct it in NetBox rather
than expecting the importer to remove it. The preview says whether the tag is already present,
included on a new parent, or will be added to an existing parent.

**Row order is lane order.** NetBox assigns the connector numbers itself, from the order the rows
appear in, and there is no column that overrides it — so the first row of a group is lane 1. Where the
switch names its breakout children `swp1s0`, `swp1s1`, …, that `sN` index is the lane number, so
ordering rows by it keeps the two views aligned. **The plan preview names the connector each row will
get: read it before applying**, especially if the sheet has been sorted.

**The importer cables the physical parent, not the children.** In the example it creates `swp1s0`…
`swp1s3` as `virtual` children of `swp1`, then puts the cable on `swp1`. The acknowledgement list
shows every child before apply. This path requires `NETBOX_AUTOCREATE_INTERFACES=true` unless all
children already exist.

**Shuffle cables use the same columns on both ends.** Keep `cable_group` as one free-text harness tag,
then put each row's physical parent in `a_parent` and/or `b_parent`. A shuffle may therefore have
several distinct parents on both sides while remaining one cable. See
`samples/3_cabling_shuffle_valid.xlsx`; its rows are the child-to-child lane map, while its NetBox
cable carries the distinct parent terminations.

**A half-built breakout is refused, not skipped.** If some of the group's ports already carry a cable
and others do not, the file is blocked and names both halves. That is deliberate: the tool only ever
creates, so it cannot add the missing lane to a cable that already exists. Finish or remove that cable
in NetBox and re-upload. When *all* of a group's ports are already on the same cable, the group is
skipped as already present — which is what makes re-uploading a sheet safe.

Leaving `cable_profile` blank on a grouped cable is allowed and produces a warning to acknowledge:
NetBox will store the cable but cannot say which lane reaches which end.

## When a port the sheet names does not exist in NetBox

By default this is simply an error: the row is blocked, the file with it, and you add the port in
NetBox before re-uploading. **That is the behaviour unless somebody has set
`NETBOX_AUTOCREATE_INTERFACES` on the server.** Check the readiness panel above the upload box — it
says `Create missing interfaces: off` or `on` — because the two behaviours look very different and
nothing else on the page tells you which one you are about to get.

With the flag on, a **device** port the sheet names and NetBox does not have is created, and the
cable is then terminated on it. Where the type comes from matters, because the tool tries two
sources in a fixed order:

1. **The device type's interface template**, if one of that name exists. Nothing is invented: you
   get the interface NetBox would have made had the template existed when the device was created.
2. **The `a_interface_type` / `b_interface_type` column on the row**, if the device type says
   nothing. Leave these blank unless a row is rejected asking for one.

If neither can answer, the row is blocked and says so. The type is never defaulted — a guess about
physical hardware, recorded in NetBox as a fact, is worse than the blocked row it would replace.

When `*_parent` is filled, this rule applies to the **physical parent** instead. The `*_port` child
does not need a supplied type: it is always created as `virtual` with the resolved parent set. If an
existing child is not virtual or belongs to a different parent, the row blocks; the importer never
reparents or re-types it.

**Fix the device type, not the sheet.** Almost every time you hit this, the cause is that somebody
added an interface template to a device type *after* the devices were built from it. NetBox copies
templates onto a device when the device is created and never again, so the template is there and the
switches do not have the port. Adding the template fixes the whole fleet at once and needs no
columns on any row; filling in `*_interface_type` on forty rows fixes forty rows.

**Read the acknowledgement list.** Every interface the plan would create is a warning you have to
acknowledge before the apply button works, naming the device, the port, the type it will be given,
and which of the two sources that type came from. This is the only place a typo gets caught: `swp1o`
and a genuinely new port are indistinguishable to the tool, and with the flag on the first one
becomes an interface in NetBox rather than an error. Read the list; it is short by design.

**What the column does when the port already exists:** nothing. The interface keeps the type NetBox
holds, and you get an informational note saying the cell was not used. This importer never changes
the type of an interface it did not create.

**Panel ports are never created**, with the flag on or off. A front port is defined by the rear port
and position it maps to, and a rear port by a position count — none of which any cabling column
expresses. An unresolved `MPO7` is still an error, and the panel still has to be built in NetBox
first.

**One thing this changes about applying.** Without interface creation/tagging, a cabling sheet was a
single all-or-nothing write. The complete order is now: create missing physical parents (including
their tag), add derived tags to existing parents, create virtual children, then create cables. A
later failure can leave an earlier interface or tag behind. Re-uploading is safe: existing tags and
interfaces are detected and only remaining work is sent. The run report and history show each
completed tag update, and the guarded revert restores its exact prior tag set when no drift occurred.

## Prepared parent locations and rack names

Before using the hierarchy page, create the site and its parent location in NetBox. Each hierarchy
row supplies `site_slug` and `parent_location_slug`; both must resolve uniquely, and this importer
never creates, renames, or moves either prerequisite.

The `datahall` and `datahall_slug` fields describe a direct child of that parent. The importer
reuses an exact match or creates it with both the selected site and parent. If the name or slug
already exists under another parent, the file is blocked instead of re-parenting the location.

Rack names are opaque join keys between the hierarchy, device, and cabling sheets. A name such as
`SYD1-DH1-R01`, `R01`, or `rack_with_underscores` is accepted as a complete value; punctuation is
not parsed into hierarchy segments. Rack names compare case-insensitively but retain the casing
supplied when created.

Location slugs use NetBox's full slug rules: letters, digits, hyphens, and underscores, up to 100
characters. The ancestry is established by NetBox relationships, not by requiring text prefixes.
Old hierarchy sheets containing `location`, `datacenter`, or `datacenter_slug` fail file-structure
validation and must be migrated to the new required columns.

## Validate, read, fix, apply

1. **Upload.** Validation reads NetBox and writes nothing at all — this is safe to do as often as you
   like, including on a sheet you know is wrong.
2. **Read the verdict.** `Ready`, `Warnings need acknowledging`, or `Blocked`, with counts: how many
   rows would create, how many would be skipped as already present, and how many objects that is.
3. **Fix the sheet** if it is blocked, and upload again. A new upload replaces the previous verdict.
4. **Acknowledge warnings** if there are any. A warning means NetBox already holds a different value
   and this tool will leave it alone — acknowledging is you confirming that is acceptable.
5. **Check the plan.** The preview names every object that would be created, and every row that would
   be skipped.
6. **Apply.** The button states the number of objects and the host they will appear on. It stays busy
   until the run finishes; do not navigate away. A validated plan is held for 30 minutes
   (`PLAN_EXPIRY_MINUTES`), after which you validate again.

### Reading an issue

Every issue names a file, a worksheet, a line, a column, the value received, and what was expected:

```
ERROR  RU_RANGE_OVERLAP
  file devices-syd1.xlsx, sheet Devices, line 47, column bottom_ru
  received "12", expected a range not overlapping rack SYD1-DH1-R01 front face
  syd1-dh1-r01-leaf01 would occupy U12-U13, already taken by line 31 (syd1-dh1-r01-leaf02)
```

Line numbers are the ones Excel shows you, with the header as line 1, so line 47 is line 47. For a
`.csv` the worksheet is reported as the file name, so the shape of an issue never varies between
formats. The code (`RU_RANGE_OVERLAP`) is stable, so if you have hundreds of issues, sort by it and
fix a class at a time.

The browser shows the first 500 issues. **Download the issue CSV** for the complete list — it is
never truncated.

### Severities

| Severity | Means | Blocks? |
| --- | --- | --- |
| ERROR | the file cannot be applied as it stands | yes, the whole file |
| WARNING | something already in NetBox differs and will be left alone | no, but must be acknowledged |
| INFO | a column was ignored, or a row will be skipped | no |

## Limits

* **2,000 data rows per file** (`MAX_APPLY_ROWS`) and **5 MB** (`MAX_UPLOAD_MB`). An apply runs inside
  one web request, so a bigger file is refused up front, naming the limit, rather than timing out
  halfway through. Split it and import the parts in order.
* Both numbers are stated on the upload panel of every page.

## When something goes wrong

### "NetBox changed after this file was validated"

Somebody else created, or occupied, something the plan depended on. NetBox is re-read immediately
before the first write, and any difference aborts the run **before anything is written**. Validate
the file again and look at the new verdict — the second validation will usually skip whatever the
other person created.

### A run aborted partway

This is the one case that needs a human. The guarantee is precise:

> Each endpoint's creates are submitted as one list and are applied all-or-nothing. The sequence
> across endpoints is not atomic: an earlier endpoint can succeed and a later one fail. Successful
> writes are journaled and can be reverted later from history after a drift check; that compensation
> is not an atomic transaction.

For a hierarchy sheet there are two write endpoints: child locations, then racks. A rack-batch
failure can therefore leave the new child locations in place while no racks were created. The
prepared sites and parent locations are never part of the run. What to do:

1. Open the run report. It lists **which endpoints completed** and every object created, with a link
   to each one in NetBox.
2. Decide whether to keep them. Usually you should: they are correct, just incomplete.
3. Fix whatever the abort reason names, then **re-upload the same sheet**. Everything already created
   is skipped, and only the remainder is created. This is the normal recovery path.
4. If you would rather start clean, use **Preflight revert** on the run. Review the exact reverse-order
   delete/restore plan, then confirm it. The action deletes only object ids created by that run and
   restores only fields that run changed.

### Reverting an applied or partly applied run

1. Open **Runs**, then open the applied run.
2. Review **Applied changes**: created and changed objects are grouped by type and retain their source
   line numbers.
3. Choose **Preflight revert**. It performs no write. It checks the target host, every recorded
   post-apply value, and NetBox's `last_updated` value.
4. If preflight passes, review the counts and reverse-order item list, then choose **Confirm revert**.

If any object changed after apply, preflight blocks the whole operation. Resolve that later work in
NetBox rather than trying to bypass the check. If NetBox rejects an inverse operation after earlier
ones succeeded, the attempt is recorded as partially reverted; run preflight again to retry only the
items still applied. A missing or ambiguous write outcome must be reconciled from the run page first.

`NETBOX_REVERT_TOKEN` is optional and defaults to `NETBOX_TOKEN`. For shared environments, configure
a separate token with read/delete and only the required field-scoped PATCH permissions, but no create
permission.

### NetBox rejected a row

NetBox's own field-level message is shown against the offending row and column. If NetBox returned a
single error that did not name a row, the batch is replayed one row at a time to find which row it
was, so the report still points at a line in your sheet.

### The page says validation is refused

The target is unreachable, unauthenticated, or running an unsupported version. The banner says which.
Nothing can be validated or applied until it is resolved — and nothing was written.

### Every device or cabling row failed to resolve something

The instance is not prepared. See [`empty-instance-prep.md`](empty-instance-prep.md); the readiness
panel at the top of each page tells you what is missing before you upload anything.

## The audit trail

**Runs** in the nav bar lists every validation and every apply, newest first, including the ones that
were refused. Each applied run shows created, changed, reverted, and still-applied counts, followed by
the object-operation journal and immutable revert attempts. Issue, result, and mutation history
reports download as CSV. Original apply records cannot be edited or deleted from the UI.
