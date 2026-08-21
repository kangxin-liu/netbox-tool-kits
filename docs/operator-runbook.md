# Operator runbook

Classification: INTERNAL. The sheets you upload describe infrastructure and are INTERNAL; the NetBox
API token is CONFIDENTIAL and belongs only in `.env` or your secret manager.

This is the day-to-day guide: how to fill a sheet in, what the verdict is telling you, and what to do
when something goes wrong. If you are pointing the tool at a NetBox that has never been used before,
read [`empty-instance-prep.md`](empty-instance-prep.md) first — sheet 1 works on an empty instance,
sheets 2 and 3 do not.

## The one thing to know

**Iteration 1 only ever creates objects.** It never updates and never deletes. If an object already
exists it is skipped, which is why re-uploading the same sheet is safe and why fixing a wrong value
in NetBox is a job for NetBox, not for this tool.

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
| 1 | Site & rack hierarchy | region, site, location, racks | one rack |
| 2 | Devices | devices mounted in racks | one device |
| 3 | Cabling | cables between devices and panels | one cable, unless rows share a `cable_group` |

Run them in that order. Sheet 2 resolves racks created by sheet 1; sheet 3 resolves devices created
by sheet 2.

## Breakout cables

A breakout is **one** cable, not several. A 400G port fanning out to four 100G ports is one physical
harness in one cage, and NetBox records it as one cable with one termination on the A side and four on
the B side. Two optional columns on the cabling sheet say so:

| Column | What it does |
| --- | --- |
| `cable_group` | Any tag you like. Every row carrying the same tag is **one cable**. Blank means the row is a cable on its own, which is how every cabling sheet worked before these columns existed |
| `cable_profile` | How the cable is built inside, which is what lets NetBox trace one lane rather than treating the cable as a single hop. The template's picklist carries the values your NetBox accepts |

One row per far end, all of them carrying the same tag:

```
a_end,          a_port, b_end,           b_port, cable_type, cable_profile,      cable_group
exa1-dh1-spine01, swp1, exa1-dh1-leaf01, swp31,  mmf-om4,    breakout-1c4p-4c1p, spine01-swp1
exa1-dh1-spine01, swp1, exa1-dh1-leaf02, swp31,  ,           ,                   spine01-swp1
exa1-dh1-spine01, swp1, exa1-dh1-leaf03, swp31,  ,           ,                   spine01-swp1
exa1-dh1-spine01, swp1, exa1-dh1-leaf04, swp31,  ,           ,                   spine01-swp1
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

**Row order is lane order.** NetBox assigns the connector numbers itself, from the order the rows
appear in, and there is no column that overrides it — so the first row of a group is lane 1. Where the
switch names its breakout children `swp1s0`, `swp1s1`, …, that `sN` index is the lane number, so
ordering rows by it keeps the two views aligned. **The plan preview names the connector each row will
get: read it before applying**, especially if the sheet has been sorted.

**Cable the physical port, not the breakout children.** If your device type defines both `swp1` and
`swp1s0`/`swp1s1`, put the cable on `swp1` and let the profile express the fan-out. Cabling the
children instead means NetBox believes there are two separate cables and will keep reporting `swp1` as
free while it is fully consumed.

**A half-built breakout is refused, not skipped.** If some of the group's ports already carry a cable
and others do not, the file is blocked and names both halves. That is deliberate: the tool only ever
creates, so it cannot add the missing lane to a cable that already exists. Finish or remove that cable
in NetBox and re-upload. When *all* of a group's ports are already on the same cable, the group is
skipped as already present — which is what makes re-uploading a sheet safe.

Leaving `cable_profile` blank on a grouped cable is allowed and produces a warning to acknowledge:
NetBox will store the cable but cannot say which lane reaches which end.

## Rack names, and the slug columns

Rack names follow `<datacenter_slug>-<datahall_slug>-<rack>`, for example `SYD1-DH1-R01`. That name
is the join key between all three sheets: sheet 2 and sheet 3 find a rack, and the devices in it, by
this name.

The first two segments are **slugs**, not display names, and the hierarchy sheet has an optional
column for each:

| Column | Fill it in when | Leave it blank when |
| --- | --- | --- |
| `datacenter_slug` | the datacenter's name is more than one word | the name is already a short token like `SYD1` |
| `datahall_slug` | the datahall's name is more than one word | the name is already `DH1` or similar |

A blank cell means the slug is derived from the name, so a sheet written before these columns existed
still validates unchanged.

Two rules about a slug you type yourself:

* It may contain letters, digits, underscores and hyphens — **except** the rack-name delimiter, which
  is `-` by default. The rack name is split on that character, so a slug containing it could not be
  read back.
* A multi-word name derives a slug containing hyphens (`Sydney One` → `sydney-one`), which is exactly
  the forbidden character. That is why a multi-word datacenter or datahall **needs its slug column
  filled in**; the file is blocked, naming the cell, if it is not.

So "Sydney One" with `datacenter_slug` = `SYD1` gives racks named `SYD1-DH1-R01` while NetBox still
shows the site as "Sydney One".

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
> across endpoints is not atomic: an earlier endpoint can succeed and a later one fail. Nothing this
> tool creates can be rolled back by this tool.

So for a hierarchy sheet, the sites can exist while the racks do not. What to do:

1. Open the run report. It lists **which endpoints completed** and every object created, with a link
   to each one in NetBox.
2. Decide whether to keep them. Usually you should: they are correct, just incomplete.
3. Fix whatever the abort reason names, then **re-upload the same sheet**. Everything already created
   is skipped, and only the remainder is created. This is the normal recovery path.
4. If you would rather start clean, delete the created objects in NetBox by hand using the report's
   links, then re-upload. This tool will not delete them for you.

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
were refused. Each run keeps its full issue list, its per-row outcomes, links to everything it
created, and who ran it. Both the issue list and the result report download as CSV. Nothing in the
history can be edited or deleted from the UI.
