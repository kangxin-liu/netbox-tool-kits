# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Tasks 6 and 7 — template generation and upload parsing.

Requirements 4.1–4.7, 5.1–5.11, 16.2, 16.3. Every fixture here is built in memory from synthetic
values; nothing reads a file from disk and nothing resembles production data.
"""

import csv
import io

import pytest
from openpyxl import Workbook, load_workbook

from importer import columns as columns_module
from importer.columns import GUIDANCE_MARKER
from importer.services import sheets
from importer.services.sheets import SheetRejected
from importer.validation import Code, Severity

SHEET_NAMES = sorted(columns_module.SHEETS)


def csv_bytes(rows: list[list[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\r\n").writerows(rows)
    return buffer.getvalue().encode("utf-8")


def xlsx_bytes(rows: list[list[object]], title: str = "Sheet1") -> bytes:
    workbook = Workbook()
    workbook.active.title = title
    for row in rows:
        workbook.active.append(row)
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def parse(importer: str, payload: bytes, filename: str, **kwargs) -> sheets.ParsedSheet:
    return sheets.parse_upload(
        importer,
        filename=filename,
        stream=io.BytesIO(payload),
        size=len(payload),
        **kwargs,
    )


def codes(issues) -> list[str]:
    return [issue.code.value for issue in issues]


def hierarchy_rows(*data: list[str]) -> bytes:
    header = [spec.label for spec in columns_module.HIERARCHY_COLUMNS]
    return csv_bytes([header, *data])


# Positional, against the D16 HIERARCHY_COLUMNS contract.
VALID_HIERARCHY_ROW = [
    "tst",
    "TST1",
    "TST1-DH1",
    "tst1-dh1",
    "TST1-DH1-R01",
    "45",
    "19",
    "active",
    "4-post-cabinet",
    "",
    "compute row",
]


# --- template generation --------------------------------------------------------------------


class TestTemplateGeneration:
    @pytest.mark.parametrize("importer", SHEET_NAMES)
    def test_csv_template_has_header_guidance_and_example(self, importer):
        text = sheets.build_template_csv(importer).decode("utf-8-sig")
        header, guidance, example = list(csv.reader(io.StringIO(text)))[:3]
        assert header == [spec.label for spec in columns_module.columns_for(importer)]
        assert guidance[0].startswith(GUIDANCE_MARKER)
        assert example == [spec.example for spec in columns_module.columns_for(importer)]

    @pytest.mark.parametrize("importer", SHEET_NAMES)
    def test_xlsx_template_names_its_worksheet_and_marks_optional_columns(self, importer):
        workbook = load_workbook(io.BytesIO(sheets.build_template_xlsx(importer)))
        sheet = workbook[columns_module.SHEET_TITLES[importer]]
        specs = columns_module.columns_for(importer)
        required_fills = set()
        optional_fills = set()
        for index, spec in enumerate(specs, start=1):
            cell = sheet.cell(row=1, column=index)
            assert cell.value == spec.label
            (required_fills if spec.required else optional_fills).add(cell.fill.fgColor.rgb)
        # Requirement 4.4: optional columns are visually distinct from required ones.
        assert required_fills.isdisjoint(optional_fills)

    @pytest.mark.parametrize("importer", SHEET_NAMES)
    def test_xlsx_template_offers_picklists_for_enumerated_fields(self, importer):
        workbook = load_workbook(io.BytesIO(sheets.build_template_xlsx(importer)))
        sheet = workbook[columns_module.SHEET_TITLES[importer]]
        formulas = " ".join(dv.formula1 for dv in sheet.data_validations.dataValidation)
        for spec in columns_module.columns_for(importer):
            if spec.choices:
                assert all(choice in formulas for choice in spec.choices), spec.key

    @pytest.mark.parametrize("importer", SHEET_NAMES)
    def test_templates_carry_no_production_looking_values(self, importer):
        for payload in (
            sheets.build_template_csv(importer),
            sheets.build_template_xlsx(importer),
        ):
            blob = payload.decode("utf-8", errors="ignore").lower()
            for real_ish in ("firmus", "syd1", "lab1", "nvidia"):
                assert real_ish not in blob

    def test_unsupported_template_format_is_named(self):
        with pytest.raises(ValueError, match="unsupported template format"):
            sheets.build_template("hierarchy", ".pdf")


class TestTemplateRoundTrip:
    """Requirement 4.2: a downloaded template is always accepted by the parser that produced it."""

    @pytest.mark.parametrize("importer", SHEET_NAMES)
    @pytest.mark.parametrize("suffix", [sheets.CSV_SUFFIX, sheets.XLSX_SUFFIX])
    def test_generated_template_parses_with_no_structural_complaint(self, importer, suffix):
        payload = sheets.build_template(importer, suffix)
        filename = sheets.template_filename(importer, suffix)
        with pytest.raises(SheetRejected) as raised:
            parse(importer, payload, filename)
        # The only complaint is the example row, which is the point of Requirement 4.5: the header
        # matched, the guidance row was skipped, and no column was missing or unknown.
        assert codes(raised.value.issues) == [
            Code.EXAMPLE_ROW_NOT_REPLACED.value,
            Code.NO_DATA_ROWS.value,
        ]

    @pytest.mark.parametrize("importer", SHEET_NAMES)
    def test_a_template_with_real_data_below_the_example_parses(self, importer):
        """Filled in as downloaded: header, guidance, example, then the operator's row."""
        payload = sheets.build_template_csv(importer).decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(payload)))
        specs = columns_module.columns_for(importer)
        operator_row = [f"synthetic-{spec.key}" for spec in specs]
        refilled = csv_bytes([*rows, operator_row])

        parsed = parse(importer, refilled, f"{importer}.csv")
        assert [row.line for row in parsed.rows] == [4]
        assert parsed.rows[0].values[specs[0].key] == f"synthetic-{specs[0].key}"
        assert codes(parsed.issues) == [Code.EXAMPLE_ROW_NOT_REPLACED.value]


# --- upload shape ---------------------------------------------------------------------------


class TestUploadShape:
    @pytest.mark.parametrize("filename", ["sheet.txt", "sheet.xls", "sheet.csv.exe", "sheet"])
    def test_other_extensions_are_rejected_naming_the_accepted_formats(self, filename):
        with pytest.raises(SheetRejected) as raised:
            parse("hierarchy", b"anything", filename)
        issue = raised.value.issues[0]
        assert issue.code is Code.UNSUPPORTED_FILE_TYPE
        assert ".csv" in issue.expected and ".xlsx" in issue.expected

    def test_an_implausible_content_type_is_rejected(self):
        with pytest.raises(SheetRejected) as raised:
            parse(
                "hierarchy",
                hierarchy_rows(VALID_HIERARCHY_ROW),
                "sheet.csv",
                content_type="application/x-msdownload",
            )
        assert raised.value.issues[0].code is Code.UNSUPPORTED_FILE_TYPE

    @pytest.mark.parametrize(
        "content_type",
        ["", "text/csv", "application/vnd.ms-excel", "application/octet-stream"],
    )
    def test_the_content_types_browsers_actually_send_are_accepted(self, content_type):
        parsed = parse(
            "hierarchy",
            hierarchy_rows(VALID_HIERARCHY_ROW),
            "sheet.csv",
            content_type=content_type,
        )
        assert len(parsed.rows) == 1

    def test_oversize_files_are_rejected_before_parsing(self):
        """Requirement 5.2: the cap applies before the bytes are read, so the body is nonsense."""
        with pytest.raises(SheetRejected) as raised:
            sheets.parse_upload(
                "hierarchy",
                filename="huge.csv",
                stream=io.BytesIO(b""),
                size=6 * 1024 * 1024,
                max_upload_mb=5,
            )
        issue = raised.value.issues[0]
        assert issue.code is Code.FILE_TOO_LARGE
        assert "5 MB" in issue.expected

    def test_an_xlsx_that_is_not_a_workbook_is_rejected(self):
        with pytest.raises(SheetRejected) as raised:
            parse("hierarchy", b"just text pretending", "sheet.xlsx")
        assert raised.value.issues[0].code is Code.UNSUPPORTED_FILE_TYPE

    def test_undecodable_csv_is_reported_not_raised(self):
        with pytest.raises(SheetRejected) as raised:
            parse("hierarchy", b"\xff\xfe\x00bad", "sheet.csv")
        assert raised.value.issues[0].code is Code.FILE_UNREADABLE

    def test_a_corrupt_xlsx_reports_no_traceback(self):
        corrupt = sheets.XLSX_MAGIC + b"not really a zip"
        with pytest.raises(SheetRejected) as raised:
            parse("hierarchy", corrupt, "sheet.xlsx")
        issue = raised.value.issues[0]
        assert issue.code is Code.FILE_UNREADABLE
        assert "Traceback" not in issue.message


# --- headers --------------------------------------------------------------------------------


class TestHeaders:
    def test_headers_match_case_insensitively_and_trimmed_in_any_order(self):
        payload = csv_bytes(
            [
                [
                    " RACK ",
                    "Rack U Height",
                    "datahall",
                    "DataHall Slug",
                    "Parent Location Slug",
                    "Site Slug",
                ],
                ["TST1-DH1-R01", "45", "TST1-DH1", "tst1-dh1", "TST1", "tst"],
            ]
        )
        parsed = parse("hierarchy", payload, "reordered.csv")
        assert parsed.rows[0].values["rack"] == "TST1-DH1-R01"
        assert parsed.rows[0].values["rack_u_height"] == "45"
        assert parsed.rows[0].values["site_slug"] == "tst"

    def test_every_missing_required_column_is_listed_at_once(self):
        payload = csv_bytes([["site_slug", "parent_location_slug"], ["tst", "TST1"]])
        with pytest.raises(SheetRejected) as raised:
            parse("hierarchy", payload, "short.csv")
        missing = {
            issue.column
            for issue in raised.value.issues
            if issue.code is Code.REQUIRED_COLUMN_MISSING
        }
        assert missing == {"datahall", "datahall_slug", "rack", "rack_u_height"}

    def test_nothing_is_parsed_when_a_required_column_is_missing(self):
        payload = csv_bytes([["location"], ["Test Region"]])
        with pytest.raises(SheetRejected) as raised:
            parse("hierarchy", payload, "short.csv")
        assert raised.value.stage == "S1"

    def test_unknown_columns_are_informational_and_ignored(self):
        header = [spec.label for spec in columns_module.HIERARCHY_COLUMNS] + ["transceiver"]
        payload = csv_bytes([header, [*VALID_HIERARCHY_ROW, "unused-optic"]])
        parsed = parse("hierarchy", payload, "extra.csv")
        unknown = [issue for issue in parsed.issues if issue.code is Code.UNKNOWN_COLUMN_IGNORED]
        assert len(unknown) == 1
        assert unknown[0].severity is Severity.INFO
        assert unknown[0].value == "transceiver"
        assert "transceiver" not in parsed.rows[0].values

    def test_a_duplicated_column_is_rejected_as_ambiguous(self):
        header = [spec.label for spec in columns_module.HIERARCHY_COLUMNS] + ["rack"]
        payload = csv_bytes([header, [*VALID_HIERARCHY_ROW, "TST1-DH1-R99"]])
        with pytest.raises(SheetRejected) as raised:
            parse("hierarchy", payload, "dupe.csv")
        assert Code.DUPLICATE_COLUMN.value in codes(raised.value.issues)


# --- rows -----------------------------------------------------------------------------------


class TestRows:
    def test_line_numbers_are_the_ones_excel_shows(self):
        """Requirement 16.2: header is line 1, so the first data row is line 2 here."""
        second = [*VALID_HIERARCHY_ROW]
        second[4] = "TST1-DH1-R02"
        payload = hierarchy_rows(VALID_HIERARCHY_ROW, second)
        parsed = parse("hierarchy", payload, "two.csv")
        assert [row.line for row in parsed.rows] == [2, 3]

    def test_blank_rows_are_skipped_wherever_they_appear(self):
        second = [*VALID_HIERARCHY_ROW]
        second[4] = "TST1-DH1-R02"
        payload = hierarchy_rows(VALID_HIERARCHY_ROW, [], ["", "", ""], second, [])
        parsed = parse("hierarchy", payload, "gappy.csv")
        assert [row.line for row in parsed.rows] == [2, 5]

    def test_a_csv_reports_its_filename_as_the_worksheet(self):
        """Requirement 16.3: issues read identically for csv and xlsx."""
        payload = csv_bytes([["location"], ["Test Region"]])
        with pytest.raises(SheetRejected) as raised:
            parse("hierarchy", payload, "hierarchy-tst1.csv")
        assert raised.value.issues[0].sheet == "hierarchy-tst1.csv"

    def test_an_all_blank_file_says_there_are_no_data_rows(self):
        payload = hierarchy_rows([], ["", ""])
        with pytest.raises(SheetRejected) as raised:
            parse("hierarchy", payload, "empty.csv")
        assert codes(raised.value.issues) == [Code.NO_DATA_ROWS.value]

    def test_cells_are_trimmed_and_kept_as_text(self):
        row = [
            "  tst  ",
            " TST1 ",
            " TST1-DH1 ",
            " tst1-dh1 ",
            "TST1-DH1-R01",
            " 45 ",
            *[""] * 5,
        ]
        parsed = parse("hierarchy", hierarchy_rows(row), "spaced.csv")
        assert parsed.rows[0].values["site_slug"] == "tst"
        assert parsed.rows[0].values["rack_u_height"] == "45"

    def test_a_cell_holding_markup_is_carried_as_text(self):
        """Requirement 5.7 / T5: the parser never interprets a cell, it only trims it."""
        row = [
            "tst",
            "TST1",
            "<script>alert(1)</script>",
            "tst1-dh1",
            "TST1-DH1-R01",
            "45",
            "",
            "",
            "",
            "",
            "",
        ]
        parsed = parse("hierarchy", hierarchy_rows(row), "markup.csv")
        assert parsed.rows[0].values["datahall"] == "<script>alert(1)</script>"

    def test_the_fingerprint_changes_with_the_file(self):
        """Requirement 10.13: the plan is bound to this."""
        one = parse("hierarchy", hierarchy_rows(VALID_HIERARCHY_ROW), "a.csv")
        again = parse("hierarchy", hierarchy_rows(VALID_HIERARCHY_ROW), "a.csv")
        edited = parse(
            "hierarchy",
            hierarchy_rows([*VALID_HIERARCHY_ROW[:5], "42", *VALID_HIERARCHY_ROW[6:]]),
            "a.csv",
        )
        assert one.fingerprint == again.fingerprint
        assert one.fingerprint != edited.fingerprint
        assert len(one.fingerprint) == 64


class TestXlsxSpecifics:
    def test_numeric_cells_do_not_arrive_as_floats(self):
        header = [spec.label for spec in columns_module.HIERARCHY_COLUMNS]
        payload = xlsx_bytes(
            [
                header,
                # Numbers as numbers: openpyxl writes 45 and 19 as floats, and they must read back
                # as the text the sheet meant.
                [
                    "tst",
                    "TST1",
                    "TST1-DH1",
                    "tst1-dh1",
                    "TST1-DH1-R01",
                    45,
                    19,
                    "active",
                    "",
                    "",
                    "",
                ],
            ]
        )
        parsed = parse("hierarchy", payload, "numbers.xlsx")
        assert parsed.rows[0].values["rack_u_height"] == "45"
        assert parsed.rows[0].values["rack_width"] == "19"

    def test_a_worksheet_named_for_the_importer_wins_over_the_first(self):
        """Requirement 5.3: first worksheet unless one is named for this importer."""
        header = [spec.label for spec in columns_module.HIERARCHY_COLUMNS]
        workbook = Workbook()
        workbook.active.title = "Notes"
        workbook.active.append(["this sheet is not the data"])
        data = workbook.create_sheet("Hierarchy")
        data.append(header)
        data.append(VALID_HIERARCHY_ROW)
        stream = io.BytesIO()
        workbook.save(stream)

        parsed = parse("hierarchy", stream.getvalue(), "named.xlsx")
        assert parsed.sheet == "Hierarchy"
        assert parsed.rows[0].values["rack"] == "TST1-DH1-R01"

    def test_the_first_worksheet_is_used_when_none_is_named_for_the_importer(self):
        header = [spec.label for spec in columns_module.HIERARCHY_COLUMNS]
        payload = xlsx_bytes([header, VALID_HIERARCHY_ROW], title="Rack build")
        parsed = parse("hierarchy", payload, "unnamed.xlsx")
        assert parsed.sheet == "Rack build"

    def test_a_formula_cell_is_never_evaluated(self):
        """Requirement 5.3 / T4: openpyxl is opened data_only, so a formula yields its stored value.

        A workbook written by this test has no cached result, so the cell reads as blank rather
        than as the formula text or an evaluated answer.
        """
        header = [spec.label for spec in columns_module.HIERARCHY_COLUMNS]
        payload = xlsx_bytes(
            [
                header,
                [
                    "tst",
                    "TST1",
                    "TST1-DH1",
                    "tst1-dh1",
                    "TST1-DH1-R01",
                    "45",
                    "",
                    "",
                    "",
                    "",
                    '=cmd|" /c calc"!A1',
                ],
            ]
        )
        parsed = parse("hierarchy", payload, "formula.xlsx")
        assert parsed.rows[0].values["description"] == ""
