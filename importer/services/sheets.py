# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Template generation and upload parsing, both driven by `columns.py`.

Generation and parsing share one column definition, so a template this module writes is always
accepted by the parser in this module (Requirement 4.2) — the round-trip test asserts exactly that.

An uploaded file is untrusted input regardless of who uploaded it (`design.md`, trust boundary 2):
the extension and the magic bytes are checked, the size cap is applied before parsing, xlsx is read
with `openpyxl` in read-only mode taking cell *values* rather than formulas, every cell is coerced
to trimmed text, and any temporary copy is deleted in a `finally` block.

Layout of a generated template, and therefore the line numbers the parser reports:

    line 1  header row
    line 2  guidance row, first cell prefixed with GUIDANCE_MARKER  → skipped on upload
    line 3  synthetic example row                                   → refused if left unchanged
    line 4+ the operator's data
"""

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from importer import columns as columns_module
from importer.columns import GUIDANCE_MARKER, ColumnSpec
from importer.validation import Code, IssueCollector, Severity, ValidationIssue

CSV_SUFFIX = ".csv"
XLSX_SUFFIX = ".xlsx"
ACCEPTED_SUFFIXES = (CSV_SUFFIX, XLSX_SUFFIX)
ACCEPTED_DESCRIPTION = "a .csv or .xlsx file"

# Browsers and Excel disagree about the content type of a .csv, so the extension is authoritative
# and the content type only has to be plausible for it.
PLAUSIBLE_CONTENT_TYPES: dict[str, frozenset[str]] = {
    CSV_SUFFIX: frozenset(
        {
            "",
            "text/csv",
            "application/csv",
            "text/plain",
            "application/vnd.ms-excel",
            "application/octet-stream",
        }
    ),
    XLSX_SUFFIX: frozenset(
        {
            "",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
            "application/zip",
            "application/octet-stream",
        }
    ),
}

XLSX_MAGIC = b"PK\x03\x04"

HEADER_ROW = 1
GUIDANCE_ROW = 2


@dataclass(frozen=True)
class Row:
    """One data row, carrying the spreadsheet line the operator sees in Excel (Requirement 16.2)."""

    line: int
    values: dict[str, str]

    def get(self, key: str) -> str:
        return self.values.get(key, "")


@dataclass
class ParsedSheet:
    importer: str
    file: str
    sheet: str
    rows: list[Row]
    fingerprint: str
    """sha256 of the uploaded bytes, which binds a plan to its file (Requirement 10.13)."""
    issues: list[ValidationIssue] = field(default_factory=list)


class SheetRejected(Exception):
    """The file could not be interpreted, so later validation stages are meaningless.

    Carries every issue found in the stage that failed, rather than the first (Requirement 10.3),
    and the stages that were therefore not reached (Requirement 10.4).
    """

    def __init__(self, issues: list[ValidationIssue], *, stage: str = "S1") -> None:
        super().__init__(f"{len(issues)} issue(s) in stage {stage}")
        self.issues = issues
        self.stage = stage


# --- template generation --------------------------------------------------------------------


def template_filename(importer: str, suffix: str) -> str:
    return f"netbox-{importer}-template{suffix}"


def _guidance_cells(specs: tuple[ColumnSpec, ...]) -> list[str]:
    cells = [spec.guidance for spec in specs]
    cells[0] = f"{GUIDANCE_MARKER} {cells[0]}"
    return cells


def build_template_csv(importer: str) -> bytes:
    specs = columns_module.columns_for(importer)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([spec.label for spec in specs])
    writer.writerow(_guidance_cells(specs))
    writer.writerow([spec.example for spec in specs])
    # utf-8-sig so Excel opens it with the right encoding on a double-click.
    return buffer.getvalue().encode("utf-8-sig")


def build_template_xlsx(importer: str) -> bytes:
    specs = columns_module.columns_for(importer)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = columns_module.SHEET_TITLES[importer]

    required_fill = PatternFill("solid", fgColor="D6E4F5")
    optional_fill = PatternFill("solid", fgColor="EDEDED")

    for index, spec in enumerate(specs, start=1):
        header = sheet.cell(row=HEADER_ROW, column=index, value=spec.label)
        header.font = Font(bold=True)
        # Requirement 4.4: optional columns are marked distinctly from required ones.
        header.fill = required_fill if spec.required else optional_fill
        header.alignment = Alignment(vertical="center")
        sheet.column_dimensions[get_column_letter(index)].width = max(14, len(spec.label) + 4)

    for index, text in enumerate(_guidance_cells(specs), start=1):
        cell = sheet.cell(row=GUIDANCE_ROW, column=index, value=text)
        cell.font = Font(italic=True, size=9, color="555555")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    sheet.row_dimensions[GUIDANCE_ROW].height = 46

    for index, spec in enumerate(specs, start=1):
        # Written as text so a value like `19` round-trips as the string the parser expects.
        sheet.cell(row=GUIDANCE_ROW + 1, column=index, value=spec.example)

    # Requirement 4.4: enumerated fields become picklists. Applied from the example row down.
    for index, spec in enumerate(specs, start=1):
        if not spec.choices:
            continue
        letter = get_column_letter(index)
        validation = DataValidation(
            type="list",
            formula1='"' + ",".join(spec.choices) + '"',
            allow_blank=not spec.required,
            showDropDown=False,
            error=f"{spec.label} must be one of: {', '.join(spec.choices)}",
            errorTitle="Not an allowed value",
        )
        sheet.add_data_validation(validation)
        validation.add(f"{letter}{GUIDANCE_ROW + 1}:{letter}1000")

    sheet.freeze_panes = f"A{GUIDANCE_ROW + 1}"

    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def build_template(importer: str, suffix: str) -> bytes:
    if suffix == CSV_SUFFIX:
        return build_template_csv(importer)
    if suffix == XLSX_SUFFIX:
        return build_template_xlsx(importer)
    raise ValueError(f"unsupported template format {suffix!r}; expected {ACCEPTED_SUFFIXES}")


# --- upload parsing -------------------------------------------------------------------------


def normalise_header(raw: Any) -> str:
    """Case-insensitive, whitespace-trimmed header matching in any column order (Req 5.4).

    Internal runs of whitespace collapse to a single underscore as well, so `Rack U Height` finds
    `rack_u_height`. That is a superset of what the requirement asks for and costs nothing.
    """
    text = "" if raw is None else str(raw)
    text = text.replace("\ufeff", "").strip().casefold()
    text = re.sub(r"[\s\-]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def coerce_cell(raw: Any) -> str:
    """Every cell is untrusted text: coerced explicitly, trimmed, never evaluated (Req 5.7).

    A float that is really an integer becomes `45` rather than `45.0`, because openpyxl reads
    numeric cells as floats and `45.0` is not what the operator typed.
    """
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, float):
        return str(int(raw)) if raw.is_integer() else repr(raw)
    if isinstance(raw, int):
        return str(raw)
    return str(raw).strip()


def _reject(collector: IssueCollector, stage: str = "S1") -> None:
    raise SheetRejected(collector.issues, stage=stage)


def _check_upload_shape(
    filename: str, size: int, content_type: str, max_upload_mb: int
) -> tuple[str, IssueCollector]:
    suffix = Path(filename).suffix.lower()
    collector = IssueCollector(file=filename, sheet=filename)

    if suffix not in ACCEPTED_SUFFIXES:
        collector.add(
            Severity.ERROR,
            Code.UNSUPPORTED_FILE_TYPE,
            value=suffix or filename,
            expected=ACCEPTED_DESCRIPTION,
            message=f"Only {ACCEPTED_DESCRIPTION} can be imported.",
        )
        _reject(collector)

    if (content_type or "").lower() not in PLAUSIBLE_CONTENT_TYPES[suffix]:
        collector.add(
            Severity.ERROR,
            Code.UNSUPPORTED_FILE_TYPE,
            value=content_type,
            expected=ACCEPTED_DESCRIPTION,
            message=(
                f"The upload claims to be {content_type!r}, which is not a plausible type for a "
                f"{suffix} file. Only {ACCEPTED_DESCRIPTION} can be imported."
            ),
        )
        _reject(collector)

    # Requirement 5.2: the size cap is applied before anything is parsed.
    limit = max_upload_mb * 1024 * 1024
    if size > limit:
        collector.add(
            Severity.ERROR,
            Code.FILE_TOO_LARGE,
            value=f"{size / 1024 / 1024:.1f} MB",
            expected=f"at most {max_upload_mb} MB",
            message=f"The file is larger than the {max_upload_mb} MB limit, so it was not parsed.",
        )
        _reject(collector)

    return suffix, collector


def _map_headers(
    importer: str, raw_headers: list[Any], collector: IssueCollector
) -> dict[int, str]:
    """Column index → column key, reporting every header problem in one pass (Req 5.5, 5.6)."""
    specs = columns_module.spec_by_key(importer)
    mapping: dict[int, str] = {}
    seen: dict[str, int] = {}

    for index, raw in enumerate(raw_headers):
        name = normalise_header(raw)
        if not name:
            continue
        if name not in specs:
            collector.add(
                Severity.INFO,
                Code.UNKNOWN_COLUMN_IGNORED,
                line=HEADER_ROW,
                column=str(raw).strip(),
                value=str(raw).strip(),
                expected="one of: " + ", ".join(specs),
                message=f"Column {str(raw).strip()!r} is not part of this sheet and was ignored.",
            )
            continue
        if name in seen:
            collector.add(
                Severity.ERROR,
                Code.DUPLICATE_COLUMN,
                line=HEADER_ROW,
                column=name,
                value=name,
                expected="one column per field",
                message=(
                    f"Column {name!r} appears twice, so its value would be ambiguous. "
                    f"Remove one of them."
                ),
                related_line=HEADER_ROW,
            )
            continue
        seen[name] = index
        mapping[index] = name

    missing = [key for key in columns_module.required_keys(importer) if key not in seen]
    if missing:
        # Every missing column at once, not the first (Requirement 5.5).
        for key in missing:
            collector.add(
                Severity.ERROR,
                Code.REQUIRED_COLUMN_MISSING,
                line=HEADER_ROW,
                column=key,
                value="(absent)",
                expected=f"a column headed {key}",
                message=f"Required column {key!r} is missing.",
            )

    if collector.errors:
        _reject(collector)
    return mapping


def _is_guidance_row(cells: list[str]) -> bool:
    return bool(cells) and cells[0].startswith(GUIDANCE_MARKER)


def _collect_rows(
    importer: str,
    raw_rows: list[tuple[int, list[Any]]],
    mapping: dict[int, str],
    collector: IssueCollector,
) -> list[Row]:
    example = columns_module.example_row(importer)
    example_signature = {key: value for key, value in example.items() if value}
    rows: list[Row] = []

    for line, raw_cells in raw_rows:
        cells = [coerce_cell(cell) for cell in raw_cells]
        if _is_guidance_row(cells):
            continue  # Requirement 5.11: the template's own guidance is not data.
        if not any(cells):
            continue  # Requirement 5.8: blank rows are skipped wherever they occur.

        values = dict.fromkeys(columns_module.spec_by_key(importer), "")
        for index, key in mapping.items():
            if index < len(cells):
                values[key] = cells[index]

        if example_signature and all(
            values.get(key) == value for key, value in example_signature.items()
        ):
            # Requirement 4.5: creating objects named after the template's examples is never right.
            collector.add(
                Severity.ERROR,
                Code.EXAMPLE_ROW_NOT_REPLACED,
                line=line,
                column=next(iter(example_signature)),
                value=values.get(next(iter(example_signature)), ""),
                expected="your own values",
                message=(
                    "This row still holds the template's example values. Replace them with real "
                    "data, or delete the row."
                ),
            )
            continue

        rows.append(Row(line=line, values=values))

    if not rows:
        collector.add(
            Severity.ERROR,
            Code.NO_DATA_ROWS,
            expected="at least one data row",
            message=(
                "The file has no data rows to import. A template counts as empty until the example "
                "row is replaced."
            ),
        )
        _reject(collector)

    return rows


def _read_csv_rows(payload: bytes, collector: IssueCollector) -> list[tuple[int, list[Any]]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        collector.add(
            Severity.ERROR,
            Code.FILE_UNREADABLE,
            expected="UTF-8 encoded text",
            message="The file is not valid UTF-8 text, so it could not be read as a .csv.",
        )
        _reject(collector)
    reader = csv.reader(io.StringIO(text, newline=""))
    return [(line, cells) for line, cells in enumerate(reader, start=HEADER_ROW)]


def _read_xlsx_rows(
    payload: bytes, importer: str, collector: IssueCollector
) -> tuple[str, list[tuple[int, list[Any]]]]:
    if not payload.startswith(XLSX_MAGIC):
        collector.add(
            Severity.ERROR,
            Code.UNSUPPORTED_FILE_TYPE,
            value="not a zip container",
            expected=ACCEPTED_DESCRIPTION,
            message=(
                "The file is named .xlsx but is not an xlsx workbook. Only "
                f"{ACCEPTED_DESCRIPTION} can be imported."
            ),
        )
        _reject(collector)

    try:
        # read_only streams rows rather than materialising the sheet (Requirement 15.1);
        # data_only takes stored cell values, so no formula is ever evaluated (Requirement 5.3).
        workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    except Exception:
        # Deliberately no exception text: an openpyxl traceback is not an operator-facing message.
        collector.add(
            Severity.ERROR,
            Code.FILE_UNREADABLE,
            expected="a readable .xlsx workbook",
            message="The workbook could not be opened. Re-save it from Excel and try again.",
        )
        _reject(collector)

    try:
        wanted = columns_module.SHEET_TITLES[importer]
        # Requirement 5.3: the first worksheet, unless one is named for this importer.
        title = next(
            (name for name in workbook.sheetnames if name.casefold() == wanted.casefold()),
            workbook.sheetnames[0] if workbook.sheetnames else "",
        )
        if not title:
            collector.add(
                Severity.ERROR,
                Code.WORKSHEET_MISSING,
                expected=f"a worksheet, ideally named {wanted}",
                message="The workbook contains no worksheets.",
            )
            _reject(collector)

        worksheet = workbook[title]
        rows = [
            (line, list(cells))
            for line, cells in enumerate(worksheet.iter_rows(values_only=True), start=HEADER_ROW)
        ]
        return title, rows
    finally:
        workbook.close()


def parse_upload(
    importer: str,
    *,
    filename: str,
    stream: BinaryIO,
    size: int,
    content_type: str = "",
    max_upload_mb: int = 5,
) -> ParsedSheet:
    """Read an uploaded sheet into rows, or raise `SheetRejected` if it cannot be interpreted.

    The caller owns the stream. `views` passes Django's `UploadedFile`, whose temporary copy is
    removed when it is closed — which the caller does in a `finally` block (Requirement 5.9).
    """
    suffix, collector = _check_upload_shape(filename, size, content_type, max_upload_mb)

    stream.seek(0)
    payload = stream.read()
    fingerprint = hashlib.sha256(payload).hexdigest()

    if suffix == CSV_SUFFIX:
        # Requirement 16.3: a csv reports its own filename as the worksheet, so an issue reads the
        # same way for both formats.
        raw_rows = _read_csv_rows(payload, collector)
        sheet_name = filename
    else:
        sheet_name, raw_rows = _read_xlsx_rows(payload, importer, collector)
        collector.sheet = sheet_name

    if not raw_rows:
        collector.add(
            Severity.ERROR,
            Code.NO_DATA_ROWS,
            expected="a header row and at least one data row",
            message="The file is empty.",
        )
        _reject(collector)

    _, header_cells = raw_rows[0]
    mapping = _map_headers(importer, list(header_cells), collector)
    rows = _collect_rows(importer, raw_rows[1:], mapping, collector)

    return ParsedSheet(
        importer=importer,
        file=filename,
        sheet=sheet_name,
        rows=rows,
        fingerprint=fingerprint,
        issues=list(collector.issues),
    )
