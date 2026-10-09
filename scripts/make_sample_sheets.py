#!/usr/bin/env python3
# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Generate sample import sheets matching the real device types in the target NetBox.

Two files per importer:

  *_valid.csv    every row should validate clean and apply successfully
  *_invalid.csv  every row after the first carries a deliberate, documented fault, to exercise
                 the validation gate and the issue-localisation format

PREREQUISITES AND ORDER — all four steps, in this order
-------------------------------------------------------
1. Run `scripts/seed_netbox.py --apply` so the device types have port templates. Devices created
   before that step get NO interfaces or panel ports, and sheet 3 will fail every row.
2. Apply 1_hierarchy — creates the region, sites, datahalls and racks these sheets reference.
3. Apply 2_devices   — creates devices in those racks, inheriting ports from step 1.
4. Apply 3_cabling   — cables the ports the devices inherited.

The `3_cabling_newports` pair is the exception and needs two more things, both described above that
section: `NETBOX_AUTOCREATE_INTERFACES=true`, and `seed_netbox.py --late-templates --apply` run
*after* step 3 so the devices predate the templates.

The `3_cabling_breakout` and `3_cabling_shuffle` pairs also need
`NETBOX_AUTOCREATE_INTERFACES=true`: their physical parents already exist, but their D13 virtual
children are intentionally created from the sheet.

Several deliberate faults in the *_invalid sheets depend on the corresponding *_valid sheet having
been applied first: "exists but differs" and "overlaps an existing device" have nothing to collide
with on an empty instance.

REAL OBJECTS THESE SHEETS ASSUME
--------------------------------
Manufacturers   Nvidia, Commscope
Device types    Spectrum-SN2201 (1U), Spectrum-SN4700 (1U), Spectrum-SN5610 (2U),
                Patch Panel - 24 pair LC-LC-SM - 1U, Patch Panel - MPO16 24Port - 1U,
                Patch Panel - 8 Group 4x Breakout 1U   (all Commscope, all 1U)
Roles (slug)    ns-leaf, ns-spine, ns-border, oob-leaf, oob-spine, patch-panel, unassigned
Interfaces      swp1..swpN, Cumulus style, as created by seed_netbox.py --naming cumulus
                SN2201  swp1-48 = 1000base-t,        swp49-52 = 100gbase-x-qsfp28
                SN4700  swp1-32 = 400gbase-x-qsfpdd
                SN5610  swp1-64 = 800gbase-x-osfp,   swp65-66 = 25gbase-x-sfp28
Panel ports     LC panel        front F1-F24    -> rear R1-R24 (1:1)
                MPO panel       front MPO1-24   -> rear MPO1-24 (1:1)
                Breakout panel  front 1A-8D     -> rear MPO1-MPO8, four fronts per trunk

Sites and racks are NOT assumed: sheet 1 creates them, so these sheets are self-contained on a
NetBox that has only the device types.

CSV is always written. XLSX is written too if openpyxl happens to be installed; it is not a
requirement of this script.

    python3 scripts/make_sample_sheets.py            # writes into samples/
    python3 scripts/make_sample_sheets.py --outdir /tmp/sheets

DUPLICATION WARNING
-------------------
The column orders below duplicate what will become importer/columns.py (spec task 5). That module
does not exist yet. When it lands, this script must import from it and these local definitions
must be deleted — the spec's "columns defined once" rule exists precisely so a template and its
parser cannot drift apart, and this file is a temporary second source of truth.

All values are synthetic: invented site codes, fake serials, no customer or production data.
The EXAMPLE- prefix is deliberately avoided; it is the sentinel that marks an unedited template
row (requirement 4.5), so real sample data must never use it.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HIERARCHY_COLUMNS = (
    "location",
    "datacenter",
    # Decision D9: the rack name's first two segments are these slugs, not the two names, so a
    # multi-word display name needs its slug column filled in.
    "datacenter_slug",
    "datahall",
    "datahall_slug",
    "rack",
    "rack_u_height",
    "rack_width",
    "rack_status",
    # NetBox 4.6: this is Rack.form_factor. Rack.type is gone, and Rack.rack_type is an FK to a
    # reusable RackType record, not a form-factor string.
    "rack_form_factor",
    "tenant",
    "description",
)

DEVICE_COLUMNS = (
    "rack",
    "rack_face",
    "bottom_ru",
    "ru_height",
    "device_name",
    "model",
    "serial",
    "manufacturer",
    "device_role",
    "status",
    "asset_tag",
    "tenant",
    # Decision D11: names live on the device type; the sheet carries addresses only.
    "loopback_ip4",
    "loopback_ip6",
    "management_ip4",
    "management_ip6",
)

CABLING_COLUMNS = (
    "a_end",
    "a_end_kind",
    "a_port",
    "a_port_side",
    "b_end",
    "b_end_kind",
    "b_port",
    "b_port_side",
    "cable_type",
    "transceiver",
    "cable_profile",
    "cable_status",
    "label",
    "length",
    "length_unit",
    "color",
)

# --------------------------------------------------------------------------------------
# Sheet 1 — hierarchy
# --------------------------------------------------------------------------------------

HIERARCHY_VALID = (
    # The first three rows leave both slug columns blank, which is the pre-D9 sheet: a single-token
    # name derives the slug the rack name already carries.
    (
        "Lab Region",
        "LAB1",
        "",
        "DH1",
        "",
        "LAB1-DH1-R01",
        "45",
        "19",
        "active",
        "4-post-cabinet",
        "",
        "compute row",
    ),
    (
        "Lab Region",
        "LAB1",
        "",
        "DH1",
        "",
        "LAB1-DH1-R02",
        "45",
        "19",
        "active",
        "4-post-cabinet",
        "",
        "second rack, same datahall",
    ),
    # New datahall under the same site, exercising location creation.
    (
        "Lab Region",
        "LAB1",
        "",
        "DH2",
        "",
        "LAB1-DH2-R01",
        "45",
        "19",
        "active",
        "4-post-cabinet",
        "",
        "new datahall under LAB1",
    ),
    # New site under the same region, and the case D9 exists for: two multi-word display names
    # whose rack name is still short, which only works because the slugs are supplied.
    (
        "Lab Region",
        "Lab Two",
        "lab2",
        "Data Hall One",
        "dh1",
        "LAB2-DH1-R01",
        "42",
        "19",
        "planned",
        "4-post-cabinet",
        "",
        "new site, multi-word names with supplied slugs",
    ),
)

HIERARCHY_INVALID = (
    (
        "Lab Region",
        "LAB1",
        "",
        "DH1",
        "",
        "LAB1-DH1-R09",
        "45",
        "19",
        "active",
        "",
        "",
        "",
        "this row is valid",
    ),
    (
        "Lab Region",
        "LAB1",
        "",
        "DH1",
        "",
        "LAB1-R05",
        "45",
        "19",
        "active",
        "",
        "",
        "",
        "malformed rack name: two segments, expects <datacenter_slug>-<datahall_slug>-<rack>",
    ),
    (
        "Lab Region",
        "LAB1",
        "",
        "DH1",
        "",
        "LAB9-DH1-R06",
        "45",
        "19",
        "active",
        "",
        "",
        "",
        "rack name's first segment (LAB9) disagrees with this row's datacenter slug (lab1)",
    ),
    # D9: a multi-word name with the slug column left blank. The derived slug is `lab-three`, which
    # contains the delimiter, and the error lands on the datacenter column because that is where the
    # operator has to act — by filling datacenter_slug in.
    (
        "Lab Region",
        "Lab Three",
        "",
        "DH1",
        "",
        "LAB3-DH1-R01",
        "45",
        "19",
        "active",
        "",
        "",
        "",
        "multi-word datacenter with no slug: the derived slug contains the rack-name delimiter",
    ),
    (
        "Lab Region",
        "LAB1",
        "",
        "DH1",
        "",
        "LAB1-DH1-R07",
        "forty-five",
        "19",
        "active",
        "",
        "",
        "",
        "rack_u_height is not an integer",
    ),
    (
        "Lab Region",
        "LAB1",
        "",
        "DH1",
        "",
        "LAB1-DH1-R09",
        "45",
        "19",
        "active",
        "",
        "",
        "",
        "duplicate of line 2 within this file",
    ),
    (
        "Lab Region",
        "LAB1",
        "",
        "DH1",
        "",
        "LAB1-DH1-R01",
        "12",
        "19",
        "active",
        "",
        "",
        "",
        "needs 1_hierarchy_valid applied first: exists with u_height 45, "
        "cannot be fixed by creating",
    ),
    (
        "Lab Region",
        "LAB1",
        "",
        "DH1",
        "",
        "LAB1-DH1-R08",
        "45",
        "19",
        "switched-on",
        "",
        "",
        "",
        "rack_status is not a NetBox choice",
    ),
)

# --------------------------------------------------------------------------------------
# Sheet 2 — devices
# --------------------------------------------------------------------------------------
PANEL_BREAKOUT = "Patch Panel - 8 Group 4x Breakout 1U"
PANEL_LC = "Patch Panel - 24 pair LC-LC-SM - 1U"


def device(
    rack: str,
    face: str,
    bottom: str,
    height: str,
    name: str,
    model: str,
    serial: str,
    manufacturer: str,
    role: str,
    status: str,
    asset: str = "",
    tenant: str = "",
    *,
    loopback_ip4: str = "",
    loopback_ip6: str = "",
    management_ip4: str = "",
    management_ip6: str = "",
    note: str = "",
) -> tuple[str, ...]:
    """One device row. Panels leave the four address cells blank; switches fill at least one.

    Addresses are RFC 5737 / RFC 3849 documentation ranges only.
    """
    row = (
        rack,
        face,
        bottom,
        height,
        name,
        model,
        serial,
        manufacturer,
        role,
        status,
        asset,
        tenant,
        loopback_ip4,
        loopback_ip6,
        management_ip4,
        management_ip6,
    )
    return (*row, note) if note else row


DEVICES_VALID = (
    # Panels at the top of the rack. Both are 1U Commscope types, no mgmt/loopback interfaces.
    device(
        "LAB1-DH1-R01",
        "front",
        "44",
        "1",
        "lab1-dh1-r01-pp01",
        PANEL_BREAKOUT,
        "",
        "Commscope",
        "patch-panel",
        "active",
    ),
    device(
        "LAB1-DH1-R01",
        "front",
        "42",
        "1",
        "lab1-dh1-r01-pp02",
        PANEL_LC,
        "",
        "Commscope",
        "patch-panel",
        "active",
    ),
    # Dual-stack on both roles: IPv4 is still the displayed primary (management_ip4).
    device(
        "LAB1-DH1-R01",
        "front",
        "1",
        "1",
        "lab1-dh1-r01-oob01",
        "Spectrum-SN2201",
        "LAB-SERIAL-0001",
        "Nvidia",
        "oob-leaf",
        "active",
        loopback_ip4="192.0.2.101/32",
        loopback_ip6="2001:db8:0:1::101/128",
        management_ip4="192.0.2.11/24",
        management_ip6="2001:db8:1::11/64",
    ),
    device(
        "LAB1-DH1-R01",
        "front",
        "10",
        "1",
        "lab1-dh1-r01-leaf01",
        "Spectrum-SN4700",
        "LAB-SERIAL-0002",
        "Nvidia",
        "ns-leaf",
        "active",
        loopback_ip4="192.0.2.102/32",
        management_ip4="192.0.2.12/24",
    ),
    # The only 2U type: ru_height must be 2 or it is an "exists but differs" style conflict.
    # Loopback only — that address is the primary, which is the no-management-IPv4 case.
    device(
        "LAB1-DH1-R01",
        "front",
        "20",
        "2",
        "lab1-dh1-r01-spine01",
        "Spectrum-SN5610",
        "LAB-SERIAL-0003",
        "Nvidia",
        "ns-spine",
        "active",
        loopback_ip4="192.0.2.103/32",
    ),
    # A second rack, so the cabling sheet can show a panel-to-panel trunk between racks.
    device(
        "LAB1-DH1-R02",
        "front",
        "44",
        "1",
        "lab1-dh1-r02-pp01",
        PANEL_BREAKOUT,
        "",
        "Commscope",
        "patch-panel",
        "active",
    ),
    device(
        "LAB1-DH1-R02",
        "front",
        "10",
        "1",
        "lab1-dh1-r02-leaf01",
        "Spectrum-SN4700",
        "",
        "Nvidia",
        "ns-leaf",
        "active",
        management_ip4="192.0.2.22/24",
    ),
    # Serial omitted: it is optional.
    device(
        "LAB1-DH1-R02",
        "front",
        "1",
        "1",
        "lab1-dh1-r02-oob01",
        "Spectrum-SN2201",
        "",
        "Nvidia",
        "oob-leaf",
        "active",
        loopback_ip4="192.0.2.201/32",
        management_ip4="192.0.2.21/24",
    ),
)

DEVICES_INVALID = (
    device(
        "LAB1-DH1-R02",
        "front",
        "30",
        "1",
        "lab1-dh1-r02-leaf08",
        "Spectrum-SN4700",
        "",
        "Nvidia",
        "ns-leaf",
        "active",
        management_ip4="192.0.2.30/24",
        note="this row is valid",
    ),
    device(
        "LAB9-DH1-R99",
        "front",
        "10",
        "1",
        "lab9-leaf01",
        "Spectrum-SN4700",
        "",
        "Nvidia",
        "ns-leaf",
        "active",
        management_ip4="192.0.2.31/24",
        note="unknown rack",
    ),
    device(
        "LAB1-DH1-R01",
        "left",
        "26",
        "1",
        "lab1-dh1-r01-leaf04",
        "Spectrum-SN4700",
        "",
        "Nvidia",
        "ns-leaf",
        "active",
        management_ip4="192.0.2.32/24",
        note="rack_face must be front or rear",
    ),
    # 45 + 2 - 1 = 46, one unit past the top of a 45U rack. ru_height matches the model, so
    # overflow is the only fault in this row.
    device(
        "LAB1-DH1-R01",
        "front",
        "45",
        "2",
        "lab1-dh1-r01-spine09",
        "Spectrum-SN5610",
        "",
        "Nvidia",
        "ns-spine",
        "active",
        loopback_ip4="192.0.2.33/32",
        note="overflows the top of the rack",
    ),
    # U10 front is occupied by leaf01 from the valid sheet.
    device(
        "LAB1-DH1-R01",
        "front",
        "10",
        "1",
        "lab1-dh1-r01-clash01",
        "Spectrum-SN4700",
        "",
        "Nvidia",
        "ns-leaf",
        "active",
        management_ip4="192.0.2.34/24",
        note="needs 2_devices_valid applied: overlaps leaf01",
    ),
    # These two overlap each other, not anything already in NetBox.
    device(
        "LAB1-DH1-R02",
        "front",
        "20",
        "2",
        "lab1-dh1-r02-spine02",
        "Spectrum-SN5610",
        "",
        "Nvidia",
        "ns-spine",
        "active",
        loopback_ip4="192.0.2.35/32",
        note="U20-U21 overlaps the next row",
    ),
    device(
        "LAB1-DH1-R02",
        "front",
        "21",
        "2",
        "lab1-dh1-r02-spine03",
        "Spectrum-SN5610",
        "",
        "Nvidia",
        "ns-spine",
        "active",
        loopback_ip4="192.0.2.36/32",
        note="U21-U22 overlaps the previous row",
    ),
    device(
        "LAB1-DH1-R02",
        "front",
        "28",
        "1",
        "lab1-dh1-r02-nope01",
        "Spectrum-SN9999",
        "",
        "Nvidia",
        "ns-leaf",
        "active",
        management_ip4="192.0.2.37/24",
        note="unknown model, autocreate is off by default",
    ),
    # SN5610 is 2U; claiming 1U would misplace the rack elevation.
    device(
        "LAB1-DH1-R02",
        "front",
        "32",
        "1",
        "lab1-dh1-r02-spine04",
        "Spectrum-SN5610",
        "",
        "Nvidia",
        "ns-spine",
        "active",
        loopback_ip4="192.0.2.38/32",
        note="ru_height 1 disagrees with the model's 2U",
    ),
    device(
        "LAB1-DH1-R02",
        "front",
        "34",
        "1",
        "lab1-dh1-r02-leaf10",
        "Spectrum-SN4700",
        "",
        "Nvidia",
        "chief-router",
        "active",
        management_ip4="192.0.2.39/24",
        note="unknown device role",
    ),
    # D11 faults. Each switch below occupies a free RU so the address rule is the only issue.
    device(
        "LAB1-DH1-R02",
        "front",
        "36",
        "1",
        "lab1-dh1-r02-leaf-noip",
        "Spectrum-SN4700",
        "",
        "Nvidia",
        "ns-leaf",
        "active",
        note="network device with no address: at least one of the four IP cells is required",
    ),
    device(
        "LAB1-DH1-R02",
        "front",
        "38",
        "1",
        "lab1-dh1-r02-pp-with-ip",
        PANEL_LC,
        "",
        "Commscope",
        "patch-panel",
        "active",
        management_ip4="192.0.2.50/24",
        note="a patch panel has nowhere to assign an address",
    ),
    device(
        "LAB1-DH1-R02",
        "front",
        "40",
        "1",
        "lab1-dh1-r02-leaf-nomask",
        "Spectrum-SN4700",
        "",
        "Nvidia",
        "ns-leaf",
        "active",
        management_ip4="192.0.2.41",
        note="host with no prefix length: the mask is not defaulted",
    ),
    device(
        "LAB1-DH1-R02",
        "front",
        "41",
        "1",
        "lab1-dh1-r02-leaf-family",
        "Spectrum-SN4700",
        "",
        "Nvidia",
        "ns-leaf",
        "active",
        management_ip4="2001:db8::1/128",
        note="IPv6 in an *_ip4 column",
    ),
    device(
        "LAB1-DH1-R02",
        "front",
        "42",
        "1",
        "lab1-dh1-r02-leaf-dup",
        "Spectrum-SN4700",
        "",
        "Nvidia",
        "ns-leaf",
        "active",
        management_ip4="192.0.2.30/24",
        note="duplicate of line 2's management_ip4",
    ),
)

# --------------------------------------------------------------------------------------
# Sheet 3 — cabling
# --------------------------------------------------------------------------------------
# Ports come from seed_netbox.py --naming cumulus:
#   SN4700  swp1-32  400gbase-x-qsfpdd     SN2201  swp1-48 1000base-t, swp49-52 100gbase-x-qsfp28
#   SN5610  swp1-64  800gbase-x-osfp, swp65-66 25gbase-x-sfp28
#   breakout panel  rear MPO1-MPO8, front 1A-8D (four fronts per trunk)
#   LC panel        rear R1-R24,    front F1-F24 (1:1)
#
# The transceiver column takes a NetBox interface type value. Supplying the type the template
# already set is a no-op that keeps these rows warning-free; a part number would need an entry in
# the transceiver map (decision D6), which ships empty.
#
# The physical story: a 400G trunk runs from a switch cage to the panel's rear MPO, and the panel's
# own front-to-rear mappings fan it out to four LC front ports. The fan-out lives in the panel, not
# in a cable, so every row here is one termination per end and single-* profiles apply.
#
# `cable_profile` used to be an ignored column here and is now real (D10), which costs these rows
# nothing: the values they already carried are genuine profiles, so they simply reach the payload.
# `transceiver` stays as the sheet's unrecognised-column demonstration (D3). A fan-out that lives in
# a *cable* rather than in a panel needs the `cable_group` column, which the breakout sample below
# carries and these rows deliberately do not.

CABLING_VALID = (
    # 400G trunk into the breakout panel's rear: one cable carrying four lanes.
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp1",
        "",
        "lab1-dh1-r01-pp01",
        "panel",
        "MPO1",
        "rear",
        "smf-os2",
        "400gbase-x-qsfpdd",
        "single-1c4p",
        "connected",
        "TRK-0001",
        "15",
        "m",
        "",
    ),
    # Two of the four breakout legs off that same trunk, front side to 100G ports.
    (
        "lab1-dh1-r01-pp01",
        "panel",
        "1A",
        "front",
        "lab1-dh1-r01-oob01",
        "device",
        "swp49",
        "",
        "smf-os2",
        "100gbase-x-qsfp28",
        "single-1c1p",
        "connected",
        "LNK-0001",
        "3",
        "m",
        "",
    ),
    (
        "lab1-dh1-r01-pp01",
        "panel",
        "1B",
        "front",
        "lab1-dh1-r01-oob01",
        "device",
        "swp50",
        "",
        "smf-os2",
        "100gbase-x-qsfp28",
        "single-1c1p",
        "connected",
        "LNK-0002",
        "3",
        "m",
        "",
    ),
    # Panel-to-panel trunk, both ends rear. No transceiver, and it must NOT warn: panel ports hold
    # no transceiver (requirement 9.8).
    (
        "lab1-dh1-r01-pp01",
        "panel",
        "MPO2",
        "rear",
        "lab1-dh1-r02-pp01",
        "panel",
        "MPO1",
        "rear",
        "smf-os2",
        "",
        "single-1c4p",
        "connected",
        "TRK-0002",
        "25",
        "m",
        "",
    ),
    # Copper between the two OOB switches: no transceiver, no profile, and no warning.
    (
        "lab1-dh1-r01-oob01",
        "device",
        "swp1",
        "",
        "lab1-dh1-r02-oob01",
        "device",
        "swp1",
        "",
        "cat6",
        "",
        "",
        "connected",
        "LNK-0003",
        "5",
        "m",
        "",
    ),
    # The 1:1 LC panel, front side, to a 25G SFP28 port on the 2U spine.
    (
        "lab1-dh1-r01-pp02",
        "panel",
        "F1",
        "front",
        "lab1-dh1-r01-spine01",
        "device",
        "swp65",
        "",
        "smf-os2",
        "25gbase-x-sfp28",
        "single-1c1p",
        "connected",
        "LNK-0004",
        "2",
        "m",
        "",
    ),
    # a_end_kind left blank: defaults to device.
    (
        "lab1-dh1-r01-leaf01",
        "",
        "swp2",
        "",
        "lab1-dh1-r02-leaf01",
        "device",
        "swp2",
        "",
        "smf-os2",
        "400gbase-x-qsfpdd",
        "single-1c4p",
        "connected",
        "LNK-0005",
        "10",
        "m",
        "",
    ),
)

CABLING_INVALID = (
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp3",
        "",
        "lab1-dh1-r01-pp01",
        "panel",
        "MPO3",
        "rear",
        "smf-os2",
        "400gbase-x-qsfpdd",
        "single-1c4p",
        "connected",
        "OK-0001",
        "15",
        "m",
        "",
        "this row is valid",
    ),
    # SN4700 has swp1-swp32 only.
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp99",
        "",
        "lab1-dh1-r01-pp01",
        "panel",
        "MPO4",
        "rear",
        "smf-os2",
        "",
        "",
        "connected",
        "BAD-0001",
        "",
        "",
        "",
        "unknown A port",
    ),
    # The breakout panel's front ports are 1A-8D; there is no 9A.
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp4",
        "",
        "lab1-dh1-r01-pp01",
        "panel",
        "9A",
        "front",
        "smf-os2",
        "",
        "",
        "connected",
        "BAD-0002",
        "",
        "",
        "",
        "unknown B port",
    ),
    (
        "lab1-dh1-r01-ghost",
        "device",
        "swp1",
        "",
        "lab1-dh1-r01-pp01",
        "panel",
        "MPO5",
        "rear",
        "smf-os2",
        "",
        "",
        "connected",
        "BAD-0003",
        "",
        "",
        "",
        "unknown A device",
    ),
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp5",
        "",
        "lab1-dh1-r01-leaf01",
        "device",
        "swp5",
        "",
        "smf-os2",
        "",
        "",
        "connected",
        "BAD-0004",
        "",
        "",
        "",
        "same port on both ends",
    ),
    # swp3 is already used by line 2 of this file.
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp3",
        "",
        "lab1-dh1-r02-pp01",
        "panel",
        "MPO2",
        "rear",
        "smf-os2",
        "",
        "",
        "connected",
        "BAD-0005",
        "",
        "",
        "",
        "termination reused within the file",
    ),
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp6",
        "",
        "lab1-dh1-r01-pp01",
        "panel",
        "MPO6",
        "rear",
        "cat99",
        "",
        "",
        "connected",
        "BAD-0006",
        "",
        "",
        "",
        "not a NetBox cable type",
    ),
    # Part number with no entry in the transceiver map (D6 ships empty).
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp7",
        "",
        "lab1-dh1-r01-pp01",
        "panel",
        "MPO7",
        "rear",
        "smf-os2",
        "QSFP-100G-SR4",
        "",
        "connected",
        "BAD-0007",
        "",
        "",
        "",
        "unmapped transceiver part number",
    ),
    # A length with no unit. The breakout profile on a lone row is *not* the fault: NetBox lets a
    # profiled cable be only partly populated, so one termination a side is legal, if pointless.
    # Before D10 this row's fault was the profile itself, back when the column was ignored entirely
    # and the row therefore carried no fault at all.
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp8",
        "",
        "lab1-dh1-r01-pp01",
        "panel",
        "MPO8",
        "rear",
        "smf-os2",
        "400gbase-x-qsfpdd",
        "breakout-1c4p-4c1p",
        "connected",
        "BAD-0008",
        "5",
        "",
        "",
        "a length with no length_unit",
    ),
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp9",
        "",
        "lab1-dh1-r02-pp01",
        "panel",
        "MPO3",
        "rear",
        "smf-os2",
        "400gbase-x-qsfpdd",
        "single-9c9p",
        "connected",
        "BAD-0009",
        "",
        "",
        "",
        "not a NetBox cable profile",
    ),
    # Optical on a device end with no transceiver: a warning to acknowledge, not an error.
    (
        "lab1-dh1-r02-leaf01",
        "device",
        "swp7",
        "",
        "lab1-dh1-r02-pp01",
        "panel",
        "MPO4",
        "rear",
        "smf-os2",
        "",
        "single-1c4p",
        "connected",
        "WARN-0001",
        "",
        "",
        "",
        "optical without transceiver on a device end (warning, not an error)",
    ),
)


# --------------------------------------------------------------------------------------
# Sheet 3b — cabling, with virtual breakout children (D10 + D13)
# --------------------------------------------------------------------------------------
# A separate sheet because these rows use both D10 grouping/profile and D13 parent/child mapping.
#
# The physical story: physical cage swp3 breaks out to four panel ports. The cable terminates on
# swp3, while swp3s0..swp3s3 are virtual children created under it. `cable_group` remains an opaque
# harness tag; the `{device}:{interface}` value belongs in `a_parent` (D13).
#
# Requires NETBOX_AUTOCREATE_INTERFACES=true because the children do not exist yet.

BREAKOUT_COLUMNS = (
    "a_end",
    "a_end_kind",
    "a_port",
    "a_parent",
    "a_interface_type",
    "b_end",
    "b_end_kind",
    "b_port",
    "b_port_side",
    "b_parent",
    "b_interface_type",
    "cable_type",
    "cable_profile",
    "cable_group",
    "cable_status",
    "label",
    "length",
    "length_unit",
)


def lane(child: str, parent: str, b_port: str, group: str, *note: str) -> tuple[str, ...]:
    """A row of a breakout group after its first: only the far end differs.

    Every cable-level cell is blank, which is the point being demonstrated — the type, profile and
    label are stated once on the group's first row and inherited (Requirement 9.18).
    """
    return (
        "lab1-dh1-r01-leaf01",
        "device",
        child,
        f"lab1-dh1-r01-leaf01:{parent}",
        "",
        "lab1-dh1-r01-pp02",
        "panel",
        b_port,
        "front",
        "",
        "",
        "",
        "",
        group,
        "",
        "",
        "",
        "",
        *note,
    )


BREAKOUT_VALID = (
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp3s0",
        "lab1-dh1-r01-leaf01:swp3",
        "",
        "lab1-dh1-r01-pp02",
        "panel",
        "F5",
        "front",
        "",
        "",
        "mmf-om4",
        "breakout-1c4p-4c1p",
        "brk-01",
        "connected",
        "BRK-0001",
        "5",
        "m",
    ),
    lane("swp3s1", "swp3", "F6", "brk-01"),
    lane("swp3s2", "swp3", "F7", "brk-01"),
    lane("swp3s3", "swp3", "F8", "brk-01"),
)

BREAKOUT_INVALID = (
    # A fifth lane on a profile with four B connectors (Requirement 9.19).
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp4s0",
        "lab1-dh1-r01-leaf01:swp4",
        "",
        "lab1-dh1-r01-pp02",
        "panel",
        "F9",
        "front",
        "",
        "",
        "mmf-om4",
        "breakout-1c4p-4c1p",
        "brk-overflow",
        "connected",
        "BRK-0002",
        "5",
        "m",
        "first of five lanes on a four-connector profile: the group overflows",
    ),
    lane("swp4s1", "swp4", "F10", "brk-overflow", "lane two"),
    lane("swp4s2", "swp4", "F11", "brk-overflow", "lane three"),
    lane("swp4s3", "swp4", "F12", "brk-overflow", "lane four, the last one that fits"),
    lane(
        "swp4s4",
        "swp4",
        "F13",
        "brk-overflow",
        "the fifth lane, which PROFILE_CONNECTOR_OVERFLOW is reported against",
    ),
    # Two rows of one cable disagreeing about what the cable is (Requirement 9.18).
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp5s0",
        "lab1-dh1-r01-leaf01:swp5",
        "",
        "lab1-dh1-r01-pp02",
        "panel",
        "F14",
        "front",
        "",
        "",
        "mmf-om4",
        "breakout-1c2p-2c1p",
        "brk-conflict",
        "connected",
        "BRK-0003",
        "5",
        "m",
        "states the cable's type and profile",
    ),
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp5s1",
        "lab1-dh1-r01-leaf01:swp5",
        "",
        "lab1-dh1-r01-pp02",
        "panel",
        "F15",
        "front",
        "",
        "",
        "smf-os2",
        "",
        "brk-conflict",
        "",
        "",
        "",
        "",
        "same cable, different cable_type: CABLE_GROUP_FIELD_CONFLICT",
    ),
    # The same A port in two different groups is still a duplicate termination (Requirement 9.17
    # relaxes 9.11 within a group, not across two of them).
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp6s0",
        "lab1-dh1-r01-leaf01:swp6",
        "",
        "lab1-dh1-r01-pp02",
        "panel",
        "F16",
        "front",
        "",
        "",
        "mmf-om4",
        "breakout-1c2p-2c1p",
        "brk-duplicate-a",
        "connected",
        "BRK-0004",
        "5",
        "m",
        "claims leaf01 swp6",
    ),
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp6s1",
        "lab1-dh1-r01-leaf01:swp6",
        "",
        "lab1-dh1-r01-pp02",
        "panel",
        "F17",
        "front",
        "",
        "",
        "mmf-om4",
        "breakout-1c2p-2c1p",
        "brk-duplicate-b",
        "connected",
        "BRK-0005",
        "5",
        "m",
        "a second cable on the same port: DUPLICATE_TERMINATION_IN_FILE",
    ),
)


# --------------------------------------------------------------------------------------
# Sheet 3c — symmetric shuffle: virtual children on both ends (D13)
# --------------------------------------------------------------------------------------
# One harness spans two physical parents on the spine side and one parent on each of two leaves.
# Every row is the human-readable child-to-child lane map; NetBox receives one cable whose
# terminations are the four distinct physical parents.


def shuffle_lane(
    a_parent: str,
    a_child: str,
    b_end: str,
    b_parent: str,
    b_child: str,
    *,
    first: bool = False,
    note: str = "",
) -> tuple[str, ...]:
    row_values = (
        "lab1-dh1-r01-spine01",
        "device",
        a_child,
        f"lab1-dh1-r01-spine01:{a_parent}",
        "",
        b_end,
        "device",
        b_child,
        "",
        f"{b_end}:{b_parent}",
        "",
        "mmf-om4" if first else "",
        "breakout-2c4p-8c1p-shuffle" if first else "",
        "shuffle-01",
        "connected" if first else "",
        "SHUF-0001" if first else "",
        "10" if first else "",
        "m" if first else "",
    )
    return row_values + ((note,) if note else ())


SHUFFLE_VALID = (
    shuffle_lane(
        "swp1",
        "swp1s0",
        "lab1-dh1-r01-leaf01",
        "swp9",
        "swp9s0",
        first=True,
    ),
    shuffle_lane(
        "swp1",
        "swp1s1",
        "lab1-dh1-r02-leaf01",
        "swp9",
        "swp9s0",
    ),
    shuffle_lane(
        "swp2",
        "swp2s0",
        "lab1-dh1-r01-leaf01",
        "swp9",
        "swp9s1",
    ),
    shuffle_lane(
        "swp2",
        "swp2s1",
        "lab1-dh1-r02-leaf01",
        "swp9",
        "swp9s1",
    ),
)

SHUFFLE_INVALID = (
    shuffle_lane(
        "swp1",
        "swp1s0",
        "lab1-dh1-r01-leaf01",
        "swp9",
        "swp9s0",
        first=True,
        note="this row is valid",
    ),
    # Explicit tuple so the malformed value is in the actual sample, not only its note.
    (
        "lab1-dh1-r01-spine01",
        "device",
        "swp1s1",
        "not-a-parent",
        "",
        "lab1-dh1-r02-leaf01",
        "device",
        "swp9s0",
        "",
        "lab1-dh1-r02-leaf01:swp9",
        "",
        "",
        "",
        "shuffle-01",
        "",
        "",
        "",
        "",
        "a_parent is malformed: expected {device}:{interface}",
    ),
    (
        "lab1-dh1-r01-spine01",
        "device",
        "swp2s0",
        "lab1-dh1-r01-leaf01:swp2",
        "",
        "lab1-dh1-r01-leaf01",
        "device",
        "swp9s1",
        "",
        "lab1-dh1-r01-leaf01:swp9",
        "",
        "",
        "",
        "shuffle-01",
        "",
        "",
        "",
        "",
        "a_parent device disagrees with a_end: BREAKOUT_PARENT_MISMATCH after S2 is fixed",
    ),
)


# --------------------------------------------------------------------------------------
# Sheet 3d — cabling onto ports that do not exist yet (D12)
# --------------------------------------------------------------------------------------
# A third cabling sheet rather than rows on either of the first two, for the reason the breakout
# pair records: these need the `*_interface_type` columns, and part of what the other samples are
# for is proving that a sheet *without* the new columns behaves exactly as it did.
#
# THESE SHEETS NEED TWO THINGS THE OTHERS DO NOT
# 1. `NETBOX_AUTOCREATE_INTERFACES=true` on the server. With it off — the default — every row here
#    is `PORT_NOT_FOUND` and the file is blocked, which is itself worth demonstrating once.
# 2. `scripts/seed_netbox.py --late-templates --apply`, run AFTER sheet 2. That adds swp33 and swp34
#    to Spectrum-SN4700, which lab1-dh1-r01-leaf01 was built from *before* they existed. NetBox
#    never retro-fits a template onto a device that already exists, so leaf01 has swp1-32 and the
#    type has swp1-34. That gap is the whole subject of D12 and is not a contrived one: it is what
#    happens to every fleet whose device types get corrected after the fact.
#
# The valid sheet shows both sources of an interface type, in the order the tool tries them:
#   * swp33 — the device type defines it, so the sheet says nothing and the template supplies
#     400gbase-x-qsfpdd. This is the case to reach for, because fixing the type fixes every device
#     of that type at once (Requirement 9.25).
#   * swp67 on the SN5610 spine — the type stops at swp66, so there is nothing to copy and the row
#     states the type itself (Requirement 9.26).
# Every row of it raises a warning to acknowledge, which is the point: with autocreate on, that
# list is the only thing between a mistyped port name and an interface in NetBox (9.28).
#
# The invalid sheet carries faults at three different stages, and a stage that fails stops the run,
# so it does NOT report all five at once — the S2 bad value hides the S3 conflict, which hides the
# three S4 faults. That is the same shape `3_cabling_invalid` has and it is worth walking through
# rather than working around: fix what is reported, re-upload, see the next stage. It is also the
# honest demonstration of why the verdict names the stages it did not reach (Requirement 10.4).

NEWPORTS_COLUMNS = (
    "a_end",
    "a_end_kind",
    "a_port",
    "a_interface_type",
    "b_end",
    "b_end_kind",
    "b_port",
    "b_port_side",
    "b_interface_type",
    "cable_type",
    "cable_status",
    "label",
)

NEWPORTS_VALID = (
    # Template-backed: swp33 exists on the type and not on the switch, so the sheet says nothing
    # about its type and gets the interface NetBox would have built with the device.
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp33",
        "",
        "lab1-dh1-r01-pp02",
        "panel",
        "F20",
        "front",
        "",
        "mmf-om4",
        "connected",
        "NEW-0001",
    ),
    # The same, on the second late template. Two interfaces, two cables, one acknowledgement each.
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp34",
        "",
        "lab1-dh1-r01-pp02",
        "panel",
        "F21",
        "front",
        "",
        "mmf-om4",
        "connected",
        "NEW-0002",
    ),
    # Column-backed: the SN5610's templates stop at swp66, so nothing can be copied and the row
    # states the type. A device-to-device row, since a panel port is never created (9.29).
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp2",
        "",
        "lab1-dh1-r01-spine01",
        "device",
        "swp67",
        "",
        "800gbase-x-osfp",
        "smf-os2",
        "connected",
        "NEW-0003",
    ),
)

NEWPORTS_INVALID = (
    # A port neither source can describe. The remedy is named twice over: add the template, or fill
    # the column. Nothing is defaulted, which is the whole of Requirement 9.26.
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp2",
        "",
        "lab1-dh1-r01-spine01",
        "device",
        "swp68",
        "",
        "",
        "smf-os2",
        "connected",
        "NEW-0004",
        "b end: type defines no swp68 and the column is blank — INTERFACE_TYPE_REQUIRED",
    ),
    # A value NetBox's interface `type` choices do not contain. Caught at S2 against the API, the
    # same way a bad cable_type is, and it would be caught with the flag off too (9.27).
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp3",
        "",
        "lab1-dh1-r01-spine01",
        "device",
        "swp69",
        "",
        "800gbase-x-wet-string",
        "smf-os2",
        "connected",
        "NEW-0005",
        "no such interface type: VALUE_NOT_IN_CHOICES lists what is accepted",
    ),
    # Two rows creating one interface and disagreeing about what it is. The tool will not choose
    # between two candidates for a fact about physical hardware (9.31).
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp4",
        "",
        "lab1-dh1-r01-spine01",
        "device",
        "swp70",
        "",
        "800gbase-x-osfp",
        "smf-os2",
        "connected",
        "NEW-0006",
        "claims spine01 swp70 is 800gbase-x-osfp",
    ),
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp5",
        "",
        "lab1-dh1-r01-spine01",
        "device",
        "swp70",
        "",
        "25gbase-x-sfp28",
        "smf-os2",
        "connected",
        "NEW-0007",
        "the same interface, a different type: INTERFACE_TYPE_CONFLICT naming both lines. Also "
        "DUPLICATE_TERMINATION_IN_FILE, because two ungrouped rows on one port are two cables",
    ),
    # A mistyped port name is indistinguishable from a new one — which is exactly why the template
    # is tried first. `swp1o` matches no template, so it lands on the blocked branch rather than
    # quietly becoming an interface.
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp1o",
        "",
        "lab1-dh1-r01-pp02",
        "panel",
        "F22",
        "front",
        "",
        "mmf-om4",
        "connected",
        "NEW-0008",
        "a typo for swp10, blocked as INTERFACE_TYPE_REQUIRED rather than created",
    ),
    # Panel ports are still never created, flag or no flag (9.29).
    (
        "lab1-dh1-r01-leaf01",
        "device",
        "swp6",
        "",
        "lab1-dh1-r01-pp02",
        "panel",
        "F99",
        "front",
        "",
        "mmf-om4",
        "connected",
        "NEW-0009",
        "no such front port, and D12 does not create panel ports: PORT_NOT_FOUND",
    ),
)


SHEETS: tuple[tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...], str], ...] = (
    (
        "1_hierarchy_valid",
        HIERARCHY_COLUMNS,
        HIERARCHY_VALID,
        "self-contained: creates the region, 2 sites, 3 datahalls and 4 racks on an empty "
        "instance; the last row exercises supplied slugs (D9)",
    ),
    (
        "1_hierarchy_invalid",
        HIERARCHY_COLUMNS,
        HIERARCHY_INVALID,
        "line 2 is valid; lines 3-9 each carry one fault, so the whole file must be blocked",
    ),
    (
        "2_devices_valid",
        DEVICE_COLUMNS,
        DEVICES_VALID,
        "needs sheet 1 applied and port templates seeded; then validates clean. Switch rows "
        "carry documentation-range CIDRs (D11); panel rows leave those cells blank",
    ),
    (
        "2_devices_invalid",
        DEVICE_COLUMNS,
        DEVICES_INVALID,
        "line 2 is valid; lines 3-16 each carry one fault, so the whole file must be blocked",
    ),
    (
        "3_cabling_valid",
        CABLING_COLUMNS,
        CABLING_VALID,
        "needs sheets 1 and 2 applied; then validates clean with no warnings to acknowledge",
    ),
    (
        "3_cabling_invalid",
        CABLING_COLUMNS,
        CABLING_INVALID,
        "line 2 is valid and line 12 is a warning; lines 3-11 are errors, so the file is blocked",
    ),
    (
        "3_cabling_breakout_valid",
        BREAKOUT_COLUMNS,
        BREAKOUT_VALID,
        "needs sheets 1 and 2 plus NETBOX_AUTOCREATE_INTERFACES=true; four virtual children under "
        "one physical parent, applied as children first and one parent-terminated cable (D13)",
    ),
    (
        "3_cabling_breakout_invalid",
        BREAKOUT_COLUMNS,
        BREAKOUT_INVALID,
        "every group here is faulty: a fifth lane on a four-connector profile, two rows of one "
        "cable disagreeing, and one port claimed by two groups",
    ),
    (
        "3_cabling_shuffle_valid",
        BREAKOUT_COLUMNS,
        SHUFFLE_VALID,
        "needs sheets 1 and 2 plus NETBOX_AUTOCREATE_INTERFACES=true; four child-to-child "
        "lane rows "
        "across several parents become one parent-terminated shuffle cable (D13)",
    ),
    (
        "3_cabling_shuffle_invalid",
        BREAKOUT_COLUMNS,
        SHUFFLE_INVALID,
        "line 2 is valid; line 3 has malformed parent syntax (S2), and line 4 names a parent on "
        "the wrong device (S3 after the S2 fault is fixed)",
    ),
    (
        "3_cabling_newports_valid",
        NEWPORTS_COLUMNS,
        NEWPORTS_VALID,
        "needs NETBOX_AUTOCREATE_INTERFACES=true and `seed_netbox.py --late-templates --apply` "
        "run after sheet 2; then every row validates with one warning to acknowledge, and applies "
        "as interfaces first and cables second (D12)",
    ),
    (
        "3_cabling_newports_invalid",
        NEWPORTS_COLUMNS,
        NEWPORTS_INVALID,
        "five ways creating an interface goes wrong, surfacing one stage at a time as the earlier "
        "ones are fixed: a type NetBox does not know (S2), two rows disagreeing about one "
        "interface (S3), then no type to be had, a typo, and a panel port (S4)",
    ),
)


def check_arity(name: str, columns: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> None:
    """Rows carry either exactly the columns, or the columns plus one trailing note.

    Without this check a miscounted row silently shifts the note into the last real column —
    which produced fixtures whose faults were not the ones they claimed to test.
    """
    allowed = {len(columns), len(columns) + 1}
    for index, row in enumerate(rows, start=2):
        if len(row) not in allowed:
            raise ValueError(
                f"{name}: row {index} has {len(row)} values; expected "
                f"{len(columns)} columns (optionally +1 trailing note)"
            )


def write_csv(path: Path, columns: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            # Any trailing note beyond the declared columns is commentary for the reader.
            writer.writerow(row[: len(columns)])


def write_xlsx(path: Path, columns: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> bool:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        return False
    book = Workbook()
    sheet = book.active
    sheet.title = path.stem[:31]
    sheet.append(list(columns))
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append(list(row[: len(columns)]))
    sheet.freeze_panes = "A2"
    for index, name in enumerate(columns, start=1):
        width = max(len(name), *(len(str(r[index - 1])) for r in rows)) + 2
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = min(
            width, 32
        )
    book.save(path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--outdir", type=Path, default=REPO_ROOT / "samples")
    args = parser.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)
    wrote_xlsx = False
    print(f"Writing sample sheets to {args.outdir}\n")

    for name, columns, rows, expectation in SHEETS:
        check_arity(name, columns, rows)
        csv_path = args.outdir / f"{name}.csv"
        write_csv(csv_path, columns, rows)
        xlsx_path = args.outdir / f"{name}.xlsx"
        if write_xlsx(xlsx_path, columns, rows):
            wrote_xlsx = True
        print(f"  {csv_path.name:<28} {len(rows)} rows — {expectation}")
        # Trailing notes are stripped from the file, so report them here: otherwise there is no
        # way to tell which fault a given line of an invalid fixture is meant to exercise.
        for line, row in enumerate(rows, start=2):
            if len(row) > len(columns) and row[-1]:
                print(f"      line {line}: {row[-1]}")

    if not wrote_xlsx:
        print("\nCSV only: openpyxl is not installed, so no .xlsx was written.")
        print("Install it later with the app's requirements; Excel opens the CSVs meanwhile.")

    print("\nOrder matters, all four steps:")
    print("  1. scripts/seed_netbox.py --apply   (port templates; devices get no ports without it)")
    print("  2. apply 1_hierarchy   creates the sites, datahalls and racks")
    print("  3. apply 2_devices     creates devices, inheriting ports from step 1")
    print("  4. apply 3_cabling     cables those ports")
    print("\nTo see D12 interface creation:")
    print("  5. scripts/seed_netbox.py --late-templates --apply   (after step 3, not before)")
    print("  6. set NETBOX_AUTOCREATE_INTERFACES=true, then apply 3_cabling_newports")
    print("\nTo see D13 parent/child breakout and shuffle maps:")
    print("  5. set NETBOX_AUTOCREATE_INTERFACES=true")
    print("  6. apply 3_cabling_breakout or 3_cabling_shuffle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
