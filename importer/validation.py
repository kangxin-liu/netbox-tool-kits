# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Validation primitives: severities, the issue record, and the issue-code registry.

Landed with task 7 rather than task 11, because a parser that cannot report an issue has nowhere to
put "these four required columns are missing". `Verdict`, `compare_to_existing`, and the stage
machinery remain task 11.

Every issue pins itself to file, worksheet, line, and column, quotes what was received and what was
expected, and carries a stable code so issues can be grouped, counted, and asserted in tests
(Requirement 16). Nothing here formats a stack trace, a request header, or a credential.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from django.conf import settings
from django.utils import timezone

from importer.columns import ColumnSpec, Conflict


class Severity(StrEnum):
    ERROR = "error"
    """Blocks the entire file. There is no partial apply."""

    WARNING = "warning"
    """Must be acknowledged before applying, but does not block."""

    INFO = "info"
    """Already-exists skips and ignored columns. Never blocks."""


class Code(StrEnum):
    """Stable machine-readable issue codes (Requirement 16.4).

    Grouped by the stage that raises them. Adding a check means adding a code here, so the set of
    things this tool can complain about is enumerable.
    """

    # S1 — file structure
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_UNREADABLE = "FILE_UNREADABLE"
    WORKSHEET_MISSING = "WORKSHEET_MISSING"
    REQUIRED_COLUMN_MISSING = "REQUIRED_COLUMN_MISSING"
    DUPLICATE_COLUMN = "DUPLICATE_COLUMN"
    UNKNOWN_COLUMN_IGNORED = "UNKNOWN_COLUMN_IGNORED"
    NO_DATA_ROWS = "NO_DATA_ROWS"
    EXAMPLE_ROW_NOT_REPLACED = "EXAMPLE_ROW_NOT_REPLACED"

    # S2 — cell format and choices
    REQUIRED_VALUE_BLANK = "REQUIRED_VALUE_BLANK"
    NOT_A_POSITIVE_INTEGER = "NOT_A_POSITIVE_INTEGER"
    VALUE_NOT_IN_CHOICES = "VALUE_NOT_IN_CHOICES"
    RACK_NAME_MALFORMED = "RACK_NAME_MALFORMED"
    COLOR_MALFORMED = "COLOR_MALFORMED"
    LENGTH_UNIT_MISSING = "LENGTH_UNIT_MISSING"
    CABLE_PROFILE_UNSUPPORTED = "CABLE_PROFILE_UNSUPPORTED"
    """The sheet named a cable profile and this NetBox reports no profile choices at all.

    Distinct from VALUE_NOT_IN_CHOICES: the value is not wrong, the target cannot hold it. Dropping
    it silently would create a cable the operator did not describe (Requirement 9.14).
    """
    SLUG_NOT_DERIVABLE = "SLUG_NOT_DERIVABLE"
    SLUG_INVALID = "SLUG_INVALID"
    """Outside NetBox's own slug charset (decision D9)."""

    SLUG_CONTAINS_DELIMITER = "SLUG_CONTAINS_DELIMITER"
    """Inside NetBox's charset but unusable in a rack name, which is split on that character.

    Distinct from SLUG_INVALID because the fix is different: one value is not a slug at all, the
    other is a perfectly good slug that this tool cannot build a rack name from (Requirement 6.6).
    """

    # S3 — intra-file consistency
    RACK_NAME_DISAGREES_WITH_COLUMNS = "RACK_NAME_DISAGREES_WITH_COLUMNS"
    DUPLICATE_RACK_IN_FILE = "DUPLICATE_RACK_IN_FILE"
    DUPLICATE_DEVICE_IN_FILE = "DUPLICATE_DEVICE_IN_FILE"
    DUPLICATE_ASSET_TAG_IN_FILE = "DUPLICATE_ASSET_TAG_IN_FILE"
    RU_RANGE_OVERLAP = "RU_RANGE_OVERLAP"
    DUPLICATE_TERMINATION_IN_FILE = "DUPLICATE_TERMINATION_IN_FILE"
    SAME_PORT_BOTH_ENDS = "SAME_PORT_BOTH_ENDS"
    CABLE_GROUP_FIELD_CONFLICT = "CABLE_GROUP_FIELD_CONFLICT"
    """Two rows of one cable group describe the same cable differently (Requirement 9.18)."""

    BREAKOUT_PARENT_MISMATCH = "BREAKOUT_PARENT_MISMATCH"
    """A panel end declares an interface parent even though only device ends support one (9.35)."""

    CHILD_PARENT_CONFLICT = "CHILD_PARENT_CONFLICT"
    """One child interface is assigned to two different parents in the same sheet (9.39)."""

    PROFILE_CONNECTOR_OVERFLOW = "PROFILE_CONNECTOR_OVERFLOW"
    """More terminations on a side than the profile has connectors there (Requirement 9.19)."""

    PROFILE_STRUCTURE_UNKNOWN = "PROFILE_STRUCTURE_UNKNOWN"
    """A profile NetBox accepts whose connector counts this tool cannot read (Requirement 9.19).

    A warning, not an error: the value is legal and NetBox will validate it: what is lost is only
    this tool's ability to catch an over-populated side before apply.
    """

    CABLE_GROUP_WITHOUT_PROFILE = "CABLE_GROUP_WITHOUT_PROFILE"
    """Several terminations on a side and no profile, so NetBox cannot trace lanes (Req 9.20)."""

    BREAKOUT_TAG_UNSUPPORTED = "BREAKOUT_TAG_UNSUPPORTED"
    """A parented profile side cannot map to an approved 2x/4x/8x tag (Requirement 9.46)."""
    SLUG_INCONSISTENT_IN_FILE = "SLUG_INCONSISTENT_IN_FILE"
    """One name given two different slugs by two rows. One object cannot hold both (Req 7.12)."""

    DEVICE_IP_INVALID = "DEVICE_IP_INVALID"
    """A loopback/management cell is not a CIDR with an explicit prefix (Requirement 8.13)."""

    DEVICE_IP_FAMILY_MISMATCH = "DEVICE_IP_FAMILY_MISMATCH"
    """An `*_ip4` cell is IPv6, or an `*_ip6` cell is IPv4 (Requirement 8.13)."""

    DUPLICATE_IP_IN_FILE = "DUPLICATE_IP_IN_FILE"
    """The same address appears in two cells of this sheet (Requirement 8.22)."""

    # S4 — cross-reference against NetBox
    RACK_NOT_FOUND = "RACK_NOT_FOUND"
    RACK_AMBIGUOUS = "RACK_AMBIGUOUS"
    SITE_PREREQUISITE_MISSING = "SITE_PREREQUISITE_MISSING"
    SITE_PREREQUISITE_AMBIGUOUS = "SITE_PREREQUISITE_AMBIGUOUS"
    PARENT_LOCATION_MISSING = "PARENT_LOCATION_MISSING"
    PARENT_LOCATION_AMBIGUOUS = "PARENT_LOCATION_AMBIGUOUS"
    PARENT_LOCATION_WRONG_SITE = "PARENT_LOCATION_WRONG_SITE"
    CHILD_LOCATION_CONFLICT = "CHILD_LOCATION_CONFLICT"
    DEVICE_TYPE_NOT_FOUND = "DEVICE_TYPE_NOT_FOUND"
    DEVICE_TYPE_WILL_BE_CREATED = "DEVICE_TYPE_WILL_BE_CREATED"
    """Autocreate is on and the model is new: a warning, so the operator reviews it (Req 8.7)."""

    MANUFACTURER_REQUIRED_FOR_AUTOCREATE = "MANUFACTURER_REQUIRED_FOR_AUTOCREATE"
    DEVICE_ROLE_NOT_FOUND = "DEVICE_ROLE_NOT_FOUND"
    TENANT_NOT_FOUND = "TENANT_NOT_FOUND"
    MANUFACTURER_NOT_FOUND = "MANUFACTURER_NOT_FOUND"
    END_NOT_FOUND = "END_NOT_FOUND"
    PORT_NOT_FOUND = "PORT_NOT_FOUND"
    PORT_ALREADY_CABLED = "PORT_ALREADY_CABLED"
    CABLE_GROUP_PARTLY_CABLED = "CABLE_GROUP_PARTLY_CABLED"
    """A multi-termination cable is half in place already (Requirement 9.22).

    Not a skip, because skipping would silently drop the lanes that are missing, and not a create,
    because adding a termination to an existing cable is an update this iteration does not do.
    """

    INTERFACE_TYPE_REQUIRED = "INTERFACE_TYPE_REQUIRED"
    """Autocreate is on, the port is missing, and neither source can say what type it is (9.26).

    Distinct from PORT_NOT_FOUND: the row could be applied, but only if somebody states the type,
    and defaulting it would write a guess into NetBox as a fact about physical hardware.
    """

    INTERFACE_TYPE_CONFLICT = "INTERFACE_TYPE_CONFLICT"
    """Two rows would create one interface with two different types (Requirement 9.31)."""

    INTERFACE_WILL_BE_CREATED = "INTERFACE_WILL_BE_CREATED"
    """A warning to acknowledge, because a typo and a new port look identical here (9.28)."""

    INTERFACE_TYPE_IGNORED = "INTERFACE_TYPE_IGNORED"
    """The port already exists, so its type is NetBox's and the cell was not used (9.27)."""

    CHILD_INTERFACE_CONFLICT = "CHILD_INTERFACE_CONFLICT"
    """An existing D13 child has the wrong parent or is not virtual (Requirement 9.36)."""

    BREAKOUT_TAG_NOT_FOUND = "BREAKOUT_TAG_NOT_FOUND"
    """The required pre-existing breakout tag slug is absent or ambiguous (Requirement 9.47)."""

    BREAKOUT_TAG_CONFLICT = "BREAKOUT_TAG_CONFLICT"
    """A parent holds a different breakout-factor tag from the profile-derived tag (9.51)."""

    SLUG_COLLISION = "SLUG_COLLISION"
    UNIQUE_FIELD_TAKEN = "UNIQUE_FIELD_TAKEN"
    RU_RANGE_EXCEEDS_RACK = "RU_RANGE_EXCEEDS_RACK"
    RU_HEIGHT_DISAGREES_WITH_TYPE = "RU_HEIGHT_DISAGREES_WITH_TYPE"
    DEVICE_IP_REQUIRED = "DEVICE_IP_REQUIRED"
    """The type defines a management or loopback interface and every IP cell is blank."""

    DEVICE_IP_NOT_APPLICABLE = "DEVICE_IP_NOT_APPLICABLE"
    """An address was supplied on a type that has nowhere to assign it (Requirement 8.14)."""

    MGMT_INTERFACE_NOT_FOUND = "MGMT_INTERFACE_NOT_FOUND"
    MGMT_INTERFACE_AMBIGUOUS = "MGMT_INTERFACE_AMBIGUOUS"
    LOOPBACK_INTERFACE_NOT_FOUND = "LOOPBACK_INTERFACE_NOT_FOUND"
    LOOPBACK_INTERFACE_AMBIGUOUS = "LOOPBACK_INTERFACE_AMBIGUOUS"
    DEVICE_INTERFACE_MISSING = "DEVICE_INTERFACE_MISSING"
    """The type defines the role interface; this existing device was never given it (8.23)."""

    IP_ALREADY_ASSIGNED = "IP_ALREADY_ASSIGNED"
    """The address exists in IPAM assigned to a different object, or unassigned (8.21)."""

    PRIMARY_IP_NOT_ON_DEVICE = "PRIMARY_IP_NOT_ON_DEVICE"
    """An explicit primary reference is not assigned to an interface on the row's device (8.18)."""

    ALREADY_EXISTS = "ALREADY_EXISTS"
    EXISTS_BUT_DIFFERS = "EXISTS_BUT_DIFFERS"
    PREREQUISITE_MISSING = "PREREQUISITE_MISSING"

    # S5 — feasibility
    ROW_COUNT_OVER_LIMIT = "ROW_COUNT_OVER_LIMIT"

    # target problems, which refuse a file without judging it
    NETBOX_REJECTED_CREATE = "NETBOX_REJECTED_CREATE"
    NETBOX_STATE_CHANGED = "NETBOX_STATE_CHANGED"
    NETBOX_UNREACHABLE = "NETBOX_UNREACHABLE"
    NETBOX_VERSION_UNSUPPORTED = "NETBOX_VERSION_UNSUPPORTED"
    """Distinct from UNREACHABLE: the host answered, and the answer was a version we refuse to
    write to (Requirement 2.7). The operator's next step is different for each."""


@dataclass(frozen=True)
class ValidationIssue:
    severity: Severity
    code: Code
    file: str
    sheet: str
    line: int | None = None
    column: str | None = None
    value: str = ""
    expected: str = ""
    message: str = ""
    related_line: int | None = None
    related_object: str | None = None
    related_url: str | None = None

    def location(self) -> str:
        return (
            f"file {self.file}, sheet {self.sheet}, "
            f"line {self.line if self.line is not None else '-'}, "
            f"column {self.column or '-'}"
        )

    @property
    def blocking(self) -> bool:
        return self.severity is Severity.ERROR


class RowError(Exception):
    """A single row-level failure, carrying everything an issue needs except its location.

    Raised by the row-level checks so that location and message formatting live in one place and
    cannot leak internals into a template (see `design.md`, "Error handling").
    """

    def __init__(
        self,
        code: Code,
        *,
        column: str | None = None,
        value: str = "",
        expected: str = "",
        message: str = "",
        related_line: int | None = None,
        related_object: str | None = None,
        related_url: str | None = None,
        severity: Severity = Severity.ERROR,
    ) -> None:
        super().__init__(message or code.value)
        self.code = code
        self.column = column
        self.value = value
        self.expected = expected
        self.message = message
        self.related_line = related_line
        self.related_object = related_object
        self.related_url = related_url
        self.severity = severity

    def as_issue(self, *, file: str, sheet: str, line: int | None) -> ValidationIssue:
        return ValidationIssue(
            severity=self.severity,
            code=self.code,
            file=file,
            sheet=sheet,
            line=line,
            column=self.column,
            value=self.value,
            expected=self.expected,
            message=self.message,
            related_line=self.related_line,
            related_object=self.related_object,
            related_url=self.related_url,
        )


@dataclass
class IssueCollector:
    """Accumulates issues for one file so a stage reports everything it found, not just the first
    problem (Requirement 10.3)."""

    file: str
    sheet: str
    issues: list[ValidationIssue] = field(default_factory=list)

    def add(
        self,
        severity: Severity,
        code: Code,
        *,
        line: int | None = None,
        column: str | None = None,
        value: str = "",
        expected: str = "",
        message: str = "",
        related_line: int | None = None,
        related_object: str | None = None,
        related_url: str | None = None,
    ) -> ValidationIssue:
        issue = ValidationIssue(
            severity=severity,
            code=code,
            file=self.file,
            sheet=self.sheet,
            line=line,
            column=column,
            value=value,
            expected=expected,
            message=message,
            related_line=related_line,
            related_object=related_object,
            related_url=related_url,
        )
        self.issues.append(issue)
        return issue

    def add_row_error(self, error: RowError, line: int | None) -> ValidationIssue:
        issue = error.as_issue(file=self.file, sheet=self.sheet, line=line)
        self.issues.append(issue)
        return issue

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity is Severity.ERROR]

    def __bool__(self) -> bool:
        return bool(self.issues)


# --- reusable cell checks (stage S2) --------------------------------------------------------


def require_value(spec: ColumnSpec, value: str) -> str:
    """A required cell must hold something (Requirement 5.6)."""
    text = str(value or "").strip()
    if not text:
        raise RowError(
            Code.REQUIRED_VALUE_BLANK,
            column=spec.key,
            value="(blank)",
            expected=spec.help_text or "a value",
            message=f"{spec.key} is required but this row leaves it blank.",
        )
    return text


def positive_integer(spec: ColumnSpec, value: str) -> int:
    """Rack heights and unit positions are counts, so 0, -1, and 1.5 are all wrong."""
    text = str(value or "").strip()
    try:
        number = int(text)
    except ValueError:
        raise RowError(
            Code.NOT_A_POSITIVE_INTEGER,
            column=spec.key,
            value=text,
            expected="a whole number greater than zero",
            message=f"{spec.key} must be a whole number greater than zero, not {text!r}.",
        ) from None
    if number < 1:
        raise RowError(
            Code.NOT_A_POSITIVE_INTEGER,
            column=spec.key,
            value=text,
            expected="a whole number greater than zero",
            message=f"{spec.key} must be greater than zero, not {number}.",
        )
    return number


def one_of(spec: ColumnSpec, value: str, allowed: frozenset[str] | tuple[str, ...]) -> str:
    """Membership of a choice set, which for NetBox fields comes from the API (Req 7.7)."""
    text = str(value or "").strip()
    permitted = {str(item) for item in allowed}
    matched = {item.casefold(): item for item in permitted}.get(text.casefold())
    if matched is None:
        raise RowError(
            Code.VALUE_NOT_IN_CHOICES,
            column=spec.key,
            value=text,
            expected="one of: " + ", ".join(sorted(permitted)),
            message=(
                f"{spec.key} {text!r} is not one of the values NetBox accepts: "
                f"{', '.join(sorted(permitted))}."
            ),
        )
    # NetBox's own spelling of the choice, so a casing difference in the sheet is accepted and
    # normalised rather than rejected.
    return matched


# --- the validation gate --------------------------------------------------------------------

STAGES: tuple[str, ...] = ("S1", "S2", "S3", "S4", "S5")
STAGE_NAMES: dict[str, str] = {
    "S1": "file structure",
    "S2": "cell format and choices",
    "S3": "consistency within the file",
    "S4": "cross-reference against NetBox",
    "S5": "plan feasibility",
}


@dataclass
class Verdict:
    """The all-or-nothing decision about one uploaded file (Requirement 10).

    Everything that can refuse an apply is answered here — errors, unreached stages, unacknowledged
    warnings, expiry, and having already been applied — so the view and the pipeline cannot disagree
    about whether a plan may be written (Requirements 10.6, 10.9, 10.15, 10.16).
    """

    file: str
    sheet: str
    fingerprint: str
    issues: list[ValidationIssue] = field(default_factory=list)
    stages_completed: tuple[str, ...] = ()
    validated_at: datetime = field(default_factory=timezone.now)
    row_count: int = 0
    create_count: int = 0
    """Rows that will create something."""

    object_count: int = 0
    """Objects those rows will create, which is the number the operator is authorising.

    Not the same as `create_count`, and the difference matters: one hierarchy row can create a
    region, a site, a location, and a rack. "1 row" and "4 objects" are different consents.
    """

    skip_count: int = 0
    blocked_count: int = 0
    acknowledged_at: datetime | None = None
    applied_at: datetime | None = None

    # --- issue tallies ----------------------------------------------------------------------

    def of(self, severity: Severity) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity is severity]

    @property
    def errors(self) -> list[ValidationIssue]:
        return self.of(Severity.ERROR)

    @property
    def warnings(self) -> list[ValidationIssue]:
        return self.of(Severity.WARNING)

    @property
    def infos(self) -> list[ValidationIssue]:
        return self.of(Severity.INFO)

    # --- stages -----------------------------------------------------------------------------

    @property
    def stages_not_reached(self) -> tuple[str, ...]:
        """Requirement 10.4: say which stages were skipped rather than implying they passed."""
        return tuple(stage for stage in STAGES if stage not in self.stages_completed)

    @property
    def complete(self) -> bool:
        return not self.stages_not_reached

    def unreached_explanation(self) -> str:
        if self.complete:
            return ""
        skipped = ", ".join(f"{stage} ({STAGE_NAMES[stage]})" for stage in self.stages_not_reached)
        return (
            f"Validation stopped early, so these stages were not reached: {skipped}. "
            f"There may be further problems they would have found."
        )

    # --- expiry -----------------------------------------------------------------------------

    @property
    def expires_at(self) -> datetime:
        return self.validated_at + timedelta(minutes=settings.PLAN_EXPIRY_MINUTES)

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or timezone.now()) >= self.expires_at

    # --- the gate ---------------------------------------------------------------------------

    @property
    def needs_acknowledgement(self) -> bool:
        """Requirement 10.9: warnings must be acknowledged explicitly, not implicitly."""
        return bool(self.warnings) and self.acknowledged_at is None

    def acknowledge(self, now: datetime | None = None) -> None:
        self.acknowledged_at = now or timezone.now()

    def refusal_reason(self, now: datetime | None = None) -> str | None:
        """Why this plan may not be applied, or None if it may.

        Ordered so the operator is told the most actionable thing first: already applied, then a
        blocking error, then expiry, then an outstanding acknowledgement.
        """
        if self.applied_at is not None:
            # Requirement 10.16.
            return (
                "This plan has already been applied. Its report shows what was created; "
                "re-uploading the sheet is the way to make further changes."
            )
        if self.errors:
            # Requirements 10.6 and 10.7: no partial apply, not even on request.
            return (
                f"{len(self.errors)} error(s) block this file. The whole file is refused — "
                f"there is no partial apply of the valid rows. Fix the sheet and upload it again."
            )
        if not self.complete:
            return self.unreached_explanation()
        if self.is_expired(now):
            return (
                f"This plan was validated at {self.validated_at:%H:%M} and expired after "
                f"{settings.PLAN_EXPIRY_MINUTES} minutes. NetBox may have changed since. "
                f"Validate the file again."
            )
        if self.needs_acknowledgement:
            return (
                f"{len(self.warnings)} warning(s) must be acknowledged before this plan can be "
                f"applied."
            )
        return None

    def is_applyable(self, now: datetime | None = None) -> bool:
        """Requirement 10.8. Used by both the apply view and the pipeline."""
        return self.refusal_reason(now) is None

    def headline(self, now: datetime | None = None) -> str:
        """The prominent verdict line (Requirement 10.10)."""
        if self.errors:
            return f"Blocked — {len(self.errors)} error(s). Nothing will be written."
        acknowledged = len(self.warnings) if self.acknowledged_at else 0
        summary = (
            f"Ready — {self.create_count} row(s) creating {self.object_count} object(s), "
            f"{self.skip_count} already present"
        )
        if self.warnings:
            summary += f", {acknowledged} of {len(self.warnings)} warning(s) acknowledged"
        reason = self.refusal_reason(now)
        return summary if reason is None else f"{summary}. {reason}"


# --- comparing a sheet value against an existing object (decision D8) -----------------------


def netbox_text(value: Any) -> str:
    """The comparable text of a value NetBox returned.

    NetBox renders a choice as `{"value": "active", "label": "Active"}` and a related object as a
    nested brief record, so a raw `str()` of either would never match what an operator typed.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        for key in ("value", "name", "model", "slug", "display"):
            if key in value and value[key] is not None:
                return netbox_text(value[key])
        return str(value.get("id", ""))
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def collapse(text: str) -> str:
    """Free text compares after trimming, collapsing whitespace, and folding case (Req 11.5b)."""
    return re.sub(r"\s+", " ", text).strip().casefold()


def _as_number(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def values_agree(spec: ColumnSpec, sheet_value: str, existing: Any) -> bool:
    """Whether a sheet value and a NetBox value mean the same thing (Requirement 11.5b)."""
    theirs = netbox_text(existing)
    ours = str(sheet_value).strip()

    if spec.numeric:
        left, right = _as_number(ours), _as_number(theirs)
        if left is not None and right is not None:
            return left == right

    if spec.choices:
        # Choice values are structured: compared exactly, bar surrounding whitespace and case, which
        # NetBox itself does not vary.
        return ours.casefold() == theirs.casefold()

    return collapse(ours) == collapse(theirs)


def compare_to_existing(
    spec: ColumnSpec,
    sheet_value: str,
    existing: Any,
    *,
    object_label: str = "the existing object",
    related_url: str | None = None,
) -> RowError | None:
    """Classify a difference between the sheet and an object that already exists (decision D8).

    Returns None when there is nothing to report: a blank cell supplies no opinion, an identity
    column cannot conflict (a different value would be a different object), and equal values are
    equal. Otherwise the column's own `Conflict` decides whether the difference blocks or warns —
    which is why `Conflict` has no default in `columns.py` (Requirement 11.5c).

    Nothing here modifies anything: iteration 1 never updates an existing object, so the sheet value
    is reported and then ignored (Requirement 11.3).
    """
    if not str(sheet_value).strip():
        return None
    if spec.conflict is Conflict.IDENTITY:
        return None
    if values_agree(spec, sheet_value, existing):
        return None

    theirs = netbox_text(existing) or "(empty)"
    severity = Severity.ERROR if spec.conflict is Conflict.CONSEQUENTIAL else Severity.WARNING
    if severity is Severity.ERROR:
        consequence = (
            "Later rows are built on this value, so the difference cannot be ignored. This "
            "iteration never updates an existing object: change the sheet to match NetBox, or "
            "change NetBox first."
        )
    else:
        consequence = (
            "Nothing else in this import depends on it. The existing value will be left as it is — "
            "acknowledge the difference to continue."
        )

    return RowError(
        Code.EXISTS_BUT_DIFFERS,
        column=spec.key,
        value=str(sheet_value).strip(),
        expected=theirs,
        message=(
            f"{object_label} already exists with {spec.key} {theirs!r}, but the sheet says "
            f"{str(sheet_value).strip()!r}. {consequence}"
        ),
        related_object=object_label,
        related_url=related_url,
        severity=severity,
    )
