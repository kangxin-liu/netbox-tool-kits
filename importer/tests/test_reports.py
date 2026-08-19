# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 16 and 18 — shaping issues for the screen, and CSVs that are safe to open.

Requirements 12.6, 16.8, 16.9, 16.10.
"""

import csv
import io

import pytest
from django.test import override_settings

from importer import reports
from importer.validation import Code, Severity, ValidationIssue


def issue(**overrides) -> ValidationIssue:
    fields = {
        "severity": Severity.ERROR,
        "code": Code.RACK_NAME_MALFORMED,
        "file": "hierarchy.csv",
        "sheet": "hierarchy.csv",
        "line": 3,
        "column": "rack",
        "value": "EXA1_DH1",
        "expected": "<datacenter_slug>-<datahall_slug>-<rack>",
        "message": "The rack name needs three segments.",
    }
    return ValidationIssue(**{**fields, **overrides})


def read_csv(payload: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(payload.decode("utf-8-sig"), newline="")))


# --- 16.9: a report is opened in Excel, so it must not execute ------------------------------


class TestFormulaEscaping:
    @pytest.mark.parametrize("leader", ["=", "+", "-", "@", "\t", "\r"])
    def test_a_value_excel_would_evaluate_is_neutralised(self, leader):
        assert reports.escape_cell(f"{leader}cmd|'/c calc'!A1").startswith("'")

    def test_an_ordinary_value_is_left_exactly_as_it_was(self):
        """The operator has to recognise the value as the one from their sheet."""
        assert reports.escape_cell("EXA1-DH1-R01") == "EXA1-DH1-R01"

    def test_a_rack_name_beginning_with_a_dash_survives_readably(self):
        """A leading `-` is a plausible typo, not an attack, and both need the same guard."""
        assert reports.escape_cell("-R01") == "'-R01"

    def test_none_becomes_empty_rather_than_the_word_none(self):
        assert reports.escape_cell(None) == ""

    def test_the_issue_csv_escapes_a_dangerous_cell(self):
        rows = read_csv(reports.issues_csv([issue(value="=1+1")]))
        assert "'=1+1" in rows[1]

    def test_the_issue_csv_escapes_a_dangerous_message_from_netbox(self):
        """NetBox's own error text lands in a cell too, and it is not ours to trust."""
        rows = read_csv(reports.issues_csv([issue(message="-see the log")]))
        assert "'-see the log" in rows[1]


class TestCsvShape:
    def test_the_issue_csv_carries_every_column_the_requirement_names(self):
        rows = read_csv(reports.issues_csv([issue()]))
        assert rows[0] == list(reports.ISSUE_HEADERS)
        for wanted in ("file", "sheet", "line", "column", "severity", "code", "expected"):
            assert wanted in rows[0]

    def test_an_issue_with_no_line_leaves_the_cell_empty_rather_than_writing_none(self):
        rows = read_csv(reports.issues_csv([issue(line=None, column=None)]))
        assert rows[1][2] == ""
        assert rows[1][3] == ""

    def test_the_csv_opens_with_a_bom_so_excel_reads_the_encoding(self):
        assert reports.issues_csv([issue()]).startswith(b"\xef\xbb\xbf")

    def test_the_issue_csv_accepts_persisted_records_as_well_as_live_issues(self, db, operator):
        """Same export either side of a restart: the DB row and the live issue carry the same
        fields, and the download should not care which it was handed."""
        from importer.models import ImportRun, ValidationIssueRecord, Verdict

        run = ImportRun.objects.create(
            importer="hierarchy",
            user=operator,
            filename="hierarchy.csv",
            netbox_host="netbox.example.com",
            verdict=Verdict.BLOCKED,
        )
        ValidationIssueRecord.objects.create(
            run=run,
            severity=str(Severity.ERROR),
            code=str(Code.RACK_NAME_MALFORMED),
            file="hierarchy.csv",
            sheet="hierarchy.csv",
            line=3,
            column="rack",
            value="=danger",
            expected="three segments",
            message="nope",
        )
        rows = read_csv(reports.issues_csv(run.issues.all()))
        assert rows[0] == list(reports.ISSUE_HEADERS)
        assert "'=danger" in rows[1]


# --- 16.8, 16.10: one systemic mistake should read as one problem ---------------------------


class TestGrouping:
    def test_issues_are_grouped_by_severity_then_line(self):
        report = reports.build_issue_report(
            [
                issue(line=5),
                issue(line=3),
                issue(severity=Severity.WARNING, line=4),
            ]
        )
        assert [group.severity for group in report.groups] == ["error", "warning"]
        assert [group.line for group in report.groups[0].lines] == [3, 5]

    def test_a_file_level_issue_sorts_after_the_numbered_lines(self):
        """It belongs to the file rather than a row, so it reads last rather than as line zero."""
        report = reports.build_issue_report([issue(line=None), issue(line=9)])
        assert [group.line for group in report.groups[0].lines] == [9, None]
        assert report.groups[0].lines[-1].label == "the file as a whole"

    def test_one_mistake_repeated_reads_as_one_code_with_a_count(self):
        report = reports.build_issue_report([issue(line=line) for line in range(3, 403)])
        assert report.code_counts == [(str(Code.RACK_NAME_MALFORMED), 400)]

    def test_codes_are_ordered_by_how_often_they_occur(self):
        report = reports.build_issue_report(
            [
                issue(code=Code.RACK_NAME_MALFORMED),
                issue(code=Code.REQUIRED_VALUE_BLANK),
                issue(code=Code.REQUIRED_VALUE_BLANK),
            ]
        )
        assert report.code_counts[0] == (str(Code.REQUIRED_VALUE_BLANK), 2)

    def test_severities_with_no_issues_are_not_rendered_as_empty_sections(self):
        report = reports.build_issue_report([issue()])
        assert [group.severity for group in report.groups] == ["error"]


class TestTruncation:
    def test_the_browser_list_is_capped_and_says_so(self):
        report = reports.build_issue_report([issue(line=n) for n in range(3, 20)], cap=5)
        assert report.shown == 5
        assert report.truncated is True
        assert "Showing the first 5 of 17 issues" in report.truncation_notice

    def test_the_count_per_code_still_covers_every_issue_when_the_list_is_capped(self):
        """The cap hides rows, not the fact that there are 17 of them (Requirement 16.10)."""
        report = reports.build_issue_report([issue(line=n) for n in range(3, 20)], cap=5)
        assert report.code_counts == [(str(Code.RACK_NAME_MALFORMED), 17)]
        assert report.total == 17

    def test_errors_are_never_crowded_out_by_a_flood_of_infos(self):
        """The cap keeps the page usable; hiding the blocking problems would defeat the point."""
        issues = [issue(severity=Severity.INFO, line=n) for n in range(3, 600)]
        issues += [issue(line=901), issue(line=902)]
        report = reports.build_issue_report(issues, cap=10)
        shown_lines = [
            group.line for group in report.groups[0].lines if report.groups[0].severity == "error"
        ]
        assert shown_lines == [901, 902]

    def test_an_uncapped_report_says_nothing_about_truncation(self):
        report = reports.build_issue_report([issue()], cap=500)
        assert report.truncated is False
        assert report.truncation_notice == ""

    @override_settings(MAX_ISSUES_DISPLAYED=2)
    def test_the_cap_defaults_to_the_configured_limit(self):
        report = reports.build_issue_report([issue(line=n) for n in range(3, 9)])
        assert report.shown == 2
        assert report.cap == 2
