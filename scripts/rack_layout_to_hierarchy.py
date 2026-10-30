"""Build a hierarchy upload sheet from a DC Technical Information rack-layout tab.

Each rack is a 5-column block whose header cell holds the rack name
(e.g. BTH01-HC07-RK0101). Device cells beneath it carry hostnames whose
type token drives the rack role:

    NSW present                   -> nvl72-rack
    RSW (SN5610 RoCE) present     -> ew-network-rack
    SVR present                   -> tan-equipment-rack
    MSW, no SN5610 Eth/Border     -> corporate-networks-rack
    ESW (SN5610 Eth) only         -> ns-network-rack
    nothing but BLANK             -> no role

AI-assisted (Cursor) — review before merge.

NVL72 racks are 52U; all others use --u-height (default 48).

Usage:
    python scripts/rack_layout_to_hierarchy.py SOURCE.xlsx "SHEET" FIRST_ROW LAST_ROW OUT.xlsx \
        [--template Hierarchy_test.xlsx]
"""

import argparse
import re

import openpyxl

RACK_RE = re.compile(r"^(?P<site>[A-Z]{3}\d{2})-(?P<hall>HC\d{2})-RK\d{4}$")
HOSTNAME_RE = re.compile(r"^[A-Z]{3}\d{2}-HC\d{2}-(?P<kind>[A-Z]{3})-")
RACK_WIDTH_COLS = 5
NVL72_U_HEIGHT = 52

COLUMNS = [
    "site_slug", "parent_location_slug", "datahall", "datahall_slug", "rack",
    "rack_role", "rack_u_height", "rack_width", "rack_status", "rack_form_factor",
    "tenant", "description",
]


def classify(kinds, texts):
    if "NSW" in kinds:
        return "nvl72-rack"
    if "RSW" in kinds or any("SN5610 RoCE" in t for t in texts):
        return "ew-network-rack"
    if "SVR" in kinds:
        return "tan-equipment-rack"
    has_sn5610_eth = any(t.startswith("SN5610") for t in texts)
    if "MSW" in kinds and not has_sn5610_eth:
        return "corporate-networks-rack"
    if "ESW" in kinds or has_sn5610_eth:
        return "ns-network-rack"
    return None


def find_racks(ws, first_row, last_row):
    headers = []
    for row in ws.iter_rows(min_row=first_row, max_row=last_row):
        for cell in row:
            if isinstance(cell.value, str) and RACK_RE.match(cell.value.strip()):
                headers.append((cell.row, cell.column, cell.value.strip()))
    header_rows = sorted({r for r, _, _ in headers})

    racks = []
    for row, col, name in headers:
        later = [r for r in header_rows if r > row]
        end = later[0] - 1 if later else last_row
        kinds, texts = set(), []
        for r in range(row + 1, end + 1):
            for c in range(col, col + RACK_WIDTH_COLS):
                value = ws.cell(r, c).value
                if not isinstance(value, str):
                    continue
                value = value.strip()
                texts.append(value)
                m = HOSTNAME_RE.match(value)
                if m:
                    kinds.add(m.group("kind"))
        racks.append((name, classify(kinds, texts), sorted(kinds)))
    return sorted(racks)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("source")
    p.add_argument("sheet")
    p.add_argument("first_row", type=int)
    p.add_argument("last_row", type=int)
    p.add_argument("out")
    p.add_argument("--template", help="xlsx whose guidance row (row 2) is copied")
    p.add_argument("--u-height", type=int, default=48)
    p.add_argument("--status", default="active")
    args = p.parse_args()

    ws = openpyxl.load_workbook(args.source, data_only=True)[args.sheet]
    racks = find_racks(ws, args.first_row, args.last_row)

    out = openpyxl.Workbook()
    sheet = out.active
    sheet.append(COLUMNS)
    if args.template:
        guidance = openpyxl.load_workbook(args.template).active[2]
        sheet.append([c.value for c in guidance])

    for name, role, kinds in racks:
        m = RACK_RE.match(name)
        site, hall = m.group("site"), m.group("hall")
        datahall = f"{site}-{hall}"
        sheet.append([
            site[:3].lower(), site.lower(), datahall, datahall.lower(), name,
            role, NVL72_U_HEIGHT if role == "nvl72-rack" else args.u_height, None, args.status, None, None, None,
        ])
        print(f"{name}  {role or '-':20}  {','.join(kinds)}")
    out.save(args.out)
    print(f"wrote {len(racks)} racks to {args.out}")


if __name__ == "__main__":
    main()
