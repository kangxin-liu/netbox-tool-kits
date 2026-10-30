"""Build a devices upload sheet from a DC Technical Information rack-layout tab.

Each rack is a 5-column block whose header cell holds the rack name (e.g. BTH01-HC07-RK0101);
the RU number column sits under the header and device text one column to its right. The first
RU row (U48) is two rows below the header, so a cell's RU is derived from its offset rather than
the RU column, which some blocks leave empty.

A switch hostname cell is the device's lowest unit. Multi-unit devices carry their description
("SN5610 ... Switch") in the cell(s) above it. The hostname's type and role letters pick the
model:

    ESW-SPF            -> SN5610      (NVIDIA, 2U)    ns-spine
    ESW-LFB            -> SN5610                      ns-border
    ESW-LFF/LFC/LFS    -> SN5610                      ns-leaf
    RSW-LF / RSW-SP    -> SN5610                      ew-leaf / ew-spine
    OSW-CR             -> SN5610                      oob-core
    OSW-LF             -> SN2201      (NVIDIA, 1U)    oob-leaf
    OSW-SP             -> SN4700      (NVIDIA, 1U)    oob-spine
    MSW-LF             -> AS4630-54TE (Edgecore, 1U)  msw-leaf
    MSW-SP             -> AS7326-56X  (Edgecore, 1U)  msw-spine

Switches outside that table, and mapped ones whose surrounding cells disagree with the model
(the unit above a 2U switch already taken, a max-power figure far from the model's), are listed
on a separate "Review" worksheet. The importer reads only the "Devices" worksheet.

AI-assisted (Cursor) — review before merge.

Usage:
    python scripts/rack_layout_to_devices.py SOURCE.xlsx "SHEET" [...] OUT.xlsx \
        [--status planned] [--kind MSW ...]
"""

import argparse
import re
import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import Font

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from importer.columns import DEVICE_COLUMNS, GUIDANCE_MARKER

RACK_RE = re.compile(r"^(?P<site>[A-Z]{3}\d{2})-(?P<hall>HC\d{2})-RK\d{4}$")
HOSTNAME_RE = re.compile(
    r"^(?P<site>[A-Z]{3}\d{2})-(?P<hall>HC\d{2})-(?P<kind>ESW|RSW|OSW|MSW)-(?P<func>[A-Z]+)\d+$"
)
SWITCH_RE = re.compile(r"(ESW|RSW|OSW|MSW)-")
TOP_RU = 48
FIRST_RU_OFFSET = 2
MAX_POWER_COL = 4

# (model, manufacturer, u_height, typical max kW as drawn on the sheet)
SN5610 = ("SN5610", "NVIDIA", 2, 2.5)
SN2201 = ("SN2201", "NVIDIA", 1, 0.15)
SN4700 = ("SN4700", "NVIDIA", 1, None)
AS4630 = ("AS4630-54TE", "Edgecore", 1, None)
AS7326 = ("AS7326-56X", "Edgecore", 1, None)

# Role slugs are children of smc-network (ESW/RSW/OSW) and corp-network (MSW). The importer
# resolves a role by its own name or slug, which NetBox keeps unique across the whole tree.
MODELS = {
    ("ESW", "SPF"): (SN5610, "ns-spine"),
    ("ESW", "LFB"): (SN5610, "ns-border"),
    ("ESW", "LFF"): (SN5610, "ns-leaf"),
    ("ESW", "LFC"): (SN5610, "ns-leaf"),
    ("ESW", "LFS"): (SN5610, "ns-leaf"),
    ("RSW", "LF"): (SN5610, "ew-leaf"),
    ("RSW", "SP"): (SN5610, "ew-spine"),
    ("RSW", None): (SN5610, "smc-network"),
    ("OSW", "LF"): (SN2201, "oob-leaf"),
    ("OSW", "CR"): (SN5610, "oob-core"),
    ("OSW", "SP"): (SN4700, "oob-spine"),
    ("MSW", "LF"): (AS4630, "msw-leaf"),
    ("MSW", "SP"): (AS7326, "msw-spine"),
}

REVIEW_COLUMNS = ["source_cell", "rack", "ru", "device_name", "model", "issue", "context"]


def model_for(kind, func):
    return MODELS.get((kind, func)) or MODELS.get((kind, None))


def is_free(text):
    return text is None or text.upper() in {"BLANK", ""} or text.upper().startswith("SN5610")


def text(ws, row, col):
    value = ws.cell(row, col).value
    return value.strip() if isinstance(value, str) else value


def find_racks(ws):
    headers = []
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and RACK_RE.match(cell.value.strip()):
                headers.append((cell.row, cell.column, cell.value.strip()))
    return headers


def scan(ws, status, kinds=None):
    devices, review = [], []
    for header_row, header_col, rack in find_racks(ws):
        col = header_col + 1
        first = header_row + FIRST_RU_OFFSET
        for row in range(first, first + TOP_RU):
            ru = TOP_RU - (row - first)
            value = text(ws, row, col)
            if not isinstance(value, str) or not SWITCH_RE.search(value):
                continue
            if kinds and SWITCH_RE.search(value).group(1) not in kinds:
                continue
            where = f"'{ws.title}'!{ws.cell(row, col).coordinate}"
            labelled = ws.cell(row, header_col).value
            m = HOSTNAME_RE.match(value)
            if not m:
                review.append([where, rack, ru, value, None, "not a recognisable hostname", None])
                continue
            spec = model_for(m.group("kind"), m.group("func"))
            if spec is None:
                above = text(ws, row - 1, col)
                review.append([
                    where, rack, ru, value, None,
                    f"no model mapped for {m.group('kind')}-{m.group('func')}",
                    f"above: {above!r}; max kW: {text(ws, row, col + MAX_POWER_COL - 1)}",
                ])
                continue
            (model, maker, height, typical_kw), role = spec

            issues = []
            if m.group("hall") != RACK_RE.match(rack).group("hall"):
                issues.append(f"hostname hall differs from rack {rack}")
            if isinstance(labelled, (int, float)) and int(labelled) != ru:
                issues.append(f"RU column says {labelled}, offset says {ru}")
            if ru + height - 1 > TOP_RU:
                issues.append(f"{height}U from U{ru} exceeds U{TOP_RU}")
            above_cells = [text(ws, row - k, col) for k in range(1, height)]
            taken = [c for c in above_cells if not is_free(c)]
            if taken:
                issues.append(f"unit above is {taken[0]!r}, not free for a {height}U {model}")
            kw = [text(ws, row - k, col + MAX_POWER_COL - 1) for k in range(height)]
            kw = next((k for k in kw if isinstance(k, (int, float))), None)
            if typical_kw and kw is not None and abs(kw - typical_kw) > typical_kw * 0.5:
                issues.append(f"sheet max power {kw} kW, {model} is drawn as {typical_kw} kW")
            if issues:
                review.append([where, rack, ru, value, model, "; ".join(issues), None])

            devices.append({
                "rack": rack, "rack_face": "front", "bottom_ru": ru, "ru_height": height,
                "device_name": value, "model": model, "manufacturer": maker,
                "device_role": role, "status": status,
            })
    devices.sort(key=lambda d: (d["rack"], -d["bottom_ru"]))
    return devices, review


def write(devices, review, out):
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "Devices"
    sheet.append([spec.label for spec in DEVICE_COLUMNS])
    guidance = [spec.guidance for spec in DEVICE_COLUMNS]
    guidance[0] = f"{GUIDANCE_MARKER} {guidance[0]}"
    sheet.append(guidance)
    for device in devices:
        sheet.append([device.get(spec.key) for spec in DEVICE_COLUMNS])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A3"

    notes = wb.create_sheet("Review")
    notes.append(REVIEW_COLUMNS)
    for cell in notes[1]:
        cell.font = Font(bold=True)
    for item in review:
        notes.append(item)
    wb.save(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("source")
    p.add_argument("sheets", nargs="+", metavar="SHEET")
    p.add_argument("out")
    p.add_argument("--status", default="planned")
    p.add_argument(
        "--kind", action="append", choices=("ESW", "RSW", "OSW", "MSW"),
        help="only switches of this hostname type; repeatable. Default: all",
    )
    args = p.parse_args()

    wb = openpyxl.load_workbook(args.source, data_only=True)
    devices, review = [], []
    for name in args.sheets:
        found, notes = scan(wb[name], args.status, args.kind)
        devices.extend(found)
        review.extend(notes)
    devices.sort(key=lambda d: (d["rack"], -d["bottom_ru"]))
    write(devices, review, args.out)
    by_role = {}
    for device in devices:
        key = f"{device['model']}/{device['device_role']}"
        by_role[key] = by_role.get(key, 0) + 1
    print(f"{', '.join(args.sheets)}: {len(devices)} devices {by_role}, {len(review)} review notes")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
