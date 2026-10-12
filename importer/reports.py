# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Shaping issues and results for the screen and for CSV.

Two jobs live here so that neither is done twice:

**Formula escaping.** A CSV is opened in Excel, and Excel treats a leading `=`, `+`, `-`, or `@`
as a formula. A cell an operator typed — a rack named `-R01`, or a NetBox error quoting one — must
not become executable when they open the report (Requirements 16.9, 12.6). Every cell of every CSV
this tool serves goes through `escape_cell`, so the guard cannot be forgotten on a new column.

**Grouping.** One systemic mistake — a delimiter typed wrong in 400 rows — should read as one
problem with a count, not as 400 problems (Requirement 16.8). `build_issue_report` groups by
severity then line, counts per code, and caps what the browser shows while the CSV keeps all of it.
"""

import csv
import io
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from importer.validation import Severity, ValidationIssue

# Excel and LibreOffice both treat these as the start of a formula. Tab and carriage return are here
# because they can smuggle a leading character past a naive check.
FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")

ISSUE_HEADERS = (
    "file",
    "sheet",
    "line",
    "column",
    "severity",
    "code",
    "received",
    "expected",
    "message",
    "related_line",
    "related_object",
    "related_url",
)

RESULT_HEADERS = (
    "sequence",
    "line",
    "outcome",
    "object_type",
    "object_name",
    "netbox_id",
    "url",
    "column",
    "detail",
)

MUTATION_HEADERS = (
    "sequence",
    "action",
    "outcome",
    "endpoint",
    "source_lines",
    "object_type",
    "object_name",
    "netbox_id",
    "url",
    "before_values",
    "after_values",
    "applied_at",
    "revert_attempt",
    "revert_outcome",
    "revert_detail",
)


def escape_cell(value: Any) -> str:
    """Neutralise a value Excel would otherwise evaluate (Requirement 16.9).

    The apostrophe prefix is Excel's own escape: the cell displays as typed and is inert. The value
    is not otherwise altered, because an operator has to recognise it as the one from their sheet.
    """
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_LEADERS):
        return "'" + text
    return text


def write_csv(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    """utf-8-sig so Excel opens it in the right encoding on a double-click."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([escape_cell(cell) for cell in row])
    return buffer.getvalue().encode("utf-8-sig")


def issues_csv(issues: Iterable[Any]) -> bytes:
    """The full validation report (Requirement 16.9).

    Accepts either a `ValidationIssue` or a persisted `ValidationIssueRecord`: the two carry the
    same fields under the same names, and the export should not care which it was handed.
    """
    return write_csv(
        ISSUE_HEADERS,
        (
            (
                issue.file,
                issue.sheet,
                issue.line if issue.line is not None else "",
                issue.column or "",
                str(issue.severity),
                str(issue.code),
                issue.value,
                issue.expected,
                issue.message,
                issue.related_line if issue.related_line is not None else "",
                issue.related_object or "",
                issue.related_url or "",
            )
            for issue in issues
        ),
    )


def results_csv(results: Iterable[Any]) -> bytes:
    """The apply report (Requirement 12.6), from either a `RowResult` or an `ImportRowResult`."""
    return write_csv(
        RESULT_HEADERS,
        (
            (
                getattr(result, "sequence", "") or "",
                result.line,
                str(result.outcome),
                # `RowResult` calls it object_kind, the model calls it object_type; same thing.
                getattr(result, "object_type", "") or getattr(result, "object_kind", ""),
                result.object_name,
                result.object_id or "",
                result.url,
                result.column,
                result.message,
            )
            for result in results
        ),
    )


def mutations_csv(mutations: Iterable[Any]) -> bytes:
    """Applied-operation and linked revert history (Requirement 18.14)."""
    rows: list[tuple[Any, ...]] = []
    for mutation in mutations:
        revert_results = list(mutation.revert_results.all())
        if not revert_results:
            revert_results = [None]
        for reverted in revert_results:
            rows.append(
                (
                    mutation.sequence,
                    mutation.action,
                    mutation.outcome,
                    mutation.endpoint,
                    ",".join(str(line) for line in mutation.source_lines),
                    mutation.object_type,
                    mutation.object_name,
                    mutation.object_id or "",
                    mutation.url,
                    json.dumps(mutation.before_values, sort_keys=True),
                    json.dumps(mutation.after_values, sort_keys=True),
                    mutation.applied_at.isoformat() if mutation.applied_at else "",
                    reverted.attempt_id if reverted is not None else "",
                    reverted.outcome if reverted is not None else "",
                    reverted.message if reverted is not None else "",
                )
            )
    return write_csv(MUTATION_HEADERS, rows)


# --- grouping for the screen ----------------------------------------------------------------


@dataclass(frozen=True)
class LineGroup:
    """The issues attached to one spreadsheet line, in the order they were found."""

    line: int | None
    issues: list[ValidationIssue]

    @property
    def label(self) -> str:
        return f"line {self.line}" if self.line is not None else "the file as a whole"


@dataclass(frozen=True)
class SeverityGroup:
    severity: str
    lines: list[LineGroup]
    total: int


@dataclass
class IssueReport:
    """What the issue list renders, already grouped, counted, and capped."""

    groups: list[SeverityGroup] = field(default_factory=list)
    code_counts: list[tuple[str, int]] = field(default_factory=list)
    total: int = 0
    shown: int = 0
    cap: int = 0

    @property
    def truncated(self) -> bool:
        return self.shown < self.total

    @property
    def truncation_notice(self) -> str:
        if not self.truncated:
            return ""
        return (
            f"Showing the first {self.shown} of {self.total} issues. The CSV download below "
            f"contains all {self.total}."
        )


SEVERITY_ORDER = (Severity.ERROR, Severity.WARNING, Severity.INFO)


def build_issue_report(issues: Sequence[Any], cap: int | None = None) -> IssueReport:
    """Group by severity then line, count per code, and cap the rendering (Req 16.8, 16.10).

    Errors are capped last, so a file with 600 infos and 3 errors still shows all 3 errors: the cap
    exists to keep the page usable, not to hide the blocking problems.
    """
    limit = settings.MAX_ISSUES_DISPLAYED if cap is None else cap

    counts: dict[str, int] = {}
    for issue in issues:
        code = str(issue.code)
        counts[code] = counts.get(code, 0) + 1

    report = IssueReport(
        total=len(issues),
        cap=limit,
        code_counts=sorted(counts.items(), key=lambda item: (-item[1], item[0])),
    )

    budget = limit
    for severity in SEVERITY_ORDER:
        matching = [issue for issue in issues if str(issue.severity) == str(severity)]
        if not matching:
            continue
        shown = matching[:budget] if budget > 0 else []
        budget -= len(shown)
        report.shown += len(shown)
        if not shown:
            # The severity still appears in the counts above; there is simply no room to list it.
            continue
        by_line: dict[int | None, list[ValidationIssue]] = {}
        for issue in shown:
            by_line.setdefault(issue.line, []).append(issue)
        lines = [
            LineGroup(line=line, issues=grouped)
            # A file-level issue has no line and reads last, rather than sorting as line zero.
            for line, grouped in sorted(
                by_line.items(), key=lambda item: (item[0] is None, item[0] or 0)
            )
        ]
        report.groups.append(
            SeverityGroup(severity=str(severity), lines=lines, total=len(matching))
        )

    return report
