# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Sheet 3 — one row per cable, between devices and patch panels (Requirement 9).

A cable is created by POSTing `a_terminations` and `b_terminations` to `dcim.cables` as arrays of
`{object_type, object_id}` (Requirement 9.6). NetBox's cable-terminations endpoint is read-only, so
nothing here writes to it. There is one endpoint in `endpoint_order`, which makes the whole sheet a
single transactional batch and leaves no half-applied window at all (decision D7).

Which port model an end resolves against is decided by `*_end_kind`, and for a panel by
`*_port_side`: a device end is an interface, a panel end is a front port unless the side says rear
(Requirements 9.1–9.3). A panel is an ordinary NetBox device, so both kinds are found the same way
and only the port model differs.

Transceivers are deferred to iteration 2 (decision D3). No column here reads or writes optic
information, and no interface's `type` is ever touched (Requirement 9.8) — the only object this
importer creates is a cable. A `transceiver` column left in an uploaded file is simply an
unrecognised column, which the parser reports as informational and drops (Requirement 5.6).

A cable's identity is its set of terminations, which is why an occupied port is a skip rather than
a create (Requirements 9.9, 11.1): the port already carries a cable, and iteration 1 never modifies
or deletes one.

**Cable groups (D10).** One row is one cable until a row fills `cable_group` in, at which point
every row sharing that tag describes one cable with several terminations — which is what a breakout
is. The group's A terminations are the distinct A-side terminations of its rows in first-appearance
order, and likewise for B (Requirement 9.16), so a fanout from one 400G port to four 100G ports is
four rows and a single POST. Row order is lane order, because NetBox derives each termination's
connector and positions from the profile and exposes them read-only: the order of the array is the
only lever the API offers (9.21).

Grouping changes three rules and it is worth naming which:

* a termination repeated across the rows of one group is a shared connector, not the duplicate that
  9.11 rejects (9.17);
* the cable's own values are read from the first row that supplies one, and a later row that
  contradicts it is rejected rather than silently losing (9.18);
* occupancy is judged over the whole group (9.22). All free creates; all held by one and the same
  cable skips, which is what makes re-uploading a sheet safe; a group that is *partly* cabled
  blocks, because completing it would mean adding a termination to an existing cable and this
  iteration modifies nothing it did not create.
"""

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

from importer.cable_profiles import structure as profile_structure
from importer.importers.base import (
    BaseImporter,
    CreateAction,
    PlannedRow,
    Registry,
    RowOutcome,
)
from importer.services.lookups import TERMINATION_ENDPOINTS, Lookups, fold
from importer.services.sheets import Row
from importer.validation import (
    Code,
    IssueCollector,
    RowError,
    Severity,
)

DEVICES = "dcim.devices"
CABLES = "dcim.cables"

DEVICE = "device"
PANEL = "panel"
FRONT = "front"
REAR = "rear"

DEFAULT_END_KIND = DEVICE
DEFAULT_PORT_SIDE = FRONT
DEFAULT_STATUS = "connected"

# (end kind, port side) → (NetBox object_type, the lookup cache's termination kind). A device end
# maps to interfaces whichever side the sheet names, because `*_port_side` describes a panel's two
# faces and a device has a single set of interfaces (Requirements 9.2, 9.3).
PORT_MODELS: dict[tuple[str, str], tuple[str, str]] = {
    (DEVICE, FRONT): ("dcim.interface", "interface"),
    (DEVICE, REAR): ("dcim.interface", "interface"),
    (PANEL, FRONT): ("dcim.frontport", "frontport"),
    (PANEL, REAR): ("dcim.rearport", "rearport"),
}

PORT_LABELS: dict[str, str] = {
    "interface": "interface",
    "frontport": "front port",
    "rearport": "rear port",
}

COLOR_PATTERN = re.compile(r"[0-9a-fA-F]{6}")

SIDES = ("a", "b")

# Columns describing the cable rather than one of its terminations. Within a group these are read
# from the first row that supplies a value, and a later row that disagrees is rejected (Req 9.18).
CABLE_COLUMNS = (
    "cable_profile",
    "cable_type",
    "cable_status",
    "label",
    "length",
    "length_unit",
    "color",
)


@dataclass
class End:
    """One side of a cable: what the sheet said, and what it resolved to."""

    side: str
    name: str
    kind: str
    port: str
    port_side: str
    object_type: str
    termination_kind: str
    line: int = 0
    """The spreadsheet line this end was read from.

    Carried on the end rather than looked up again later, so an issue about one termination of a
    four-row breakout points at the row that named it (Requirement 16.2).
    """

    device: dict[str, Any] | None = None
    termination: dict[str, Any] | None = None

    @property
    def label(self) -> str:
        return f"{self.side.upper()} end"

    @property
    def end_column(self) -> str:
        return f"{self.side}_end"

    @property
    def port_column(self) -> str:
        return f"{self.side}_port"

    @property
    def port_label(self) -> str:
        return PORT_LABELS[self.termination_kind]

    @property
    def signature(self) -> tuple[str, str, str]:
        """What makes two ends the same termination, judged before NetBox is consulted.

        S3 runs before anything has been resolved, so sameness is decided on the sheet's own
        values: the port model the kind and side select, the device name, and the port name
        (Requirements 9.10, 9.11).
        """
        return (self.object_type, fold(self.name), fold(self.port))

    @property
    def description(self) -> str:
        return f"{self.port_label} {self.port!r} on {self.name!r}"

    @property
    def object_id(self) -> int | None:
        return int(self.termination["id"]) if self.termination else None


def read_end(row: Row, side: str) -> End:
    """One end of a row with the documented defaults applied (Requirements 9.1, 9.3).

    `*_end_kind` is declared required in `columns.py`, so in practice a blank cell is rejected at
    S2 before the `device` default below is ever reached. The default is implemented anyway because
    9.1 specifies it: relaxing the column to optional is then the only change needed.
    """
    kind = fold(row.get(f"{side}_end_kind")) or DEFAULT_END_KIND
    port_side = fold(row.get(f"{side}_port_side")) or DEFAULT_PORT_SIDE
    object_type, termination_kind = PORT_MODELS[(kind, port_side)]
    return End(
        side=side,
        name=row.get(f"{side}_end").strip(),
        kind=kind,
        port=row.get(f"{side}_port").strip(),
        port_side=port_side,
        object_type=object_type,
        termination_kind=termination_kind,
        line=row.line,
    )


def describe_cable(cable: dict[str, Any]) -> str:
    """Name an existing cable well enough for an operator to find it (Requirement 16.7)."""
    label = str(cable.get("label") or cable.get("display") or "").strip()
    return f"Cable #{cable.get('id')}" + (f" ({label})" if label else "")


@dataclass
class Occupancy:
    """What already holds a prospective cable's terminations (Requirements 9.9, 9.22)."""

    multi: bool = False
    """Whether either side carries more than one termination, i.e. whether this is a real group."""

    free: list[End] = field(default_factory=list)
    taken: list[tuple[End, dict[str, Any]]] = field(default_factory=list)
    holders: dict[int, dict[str, Any]] = field(default_factory=dict)

    @property
    def fully_held(self) -> bool:
        """Every termination already on one and the same cable: this cable exists (Req 11.1)."""
        return bool(self.taken) and not self.free and len(self.holders) == 1

    @property
    def blocks(self) -> bool:
        """Requirement 9.22: a multi-termination cable that is only partly in place.

        With one termination a side, a skip loses nothing — the link is simply cabled differently
        already. With several, a skip would silently drop the lanes that are missing, which is the
        difference this property exists to draw.
        """
        return bool(self.taken) and self.multi and not self.fully_held


def _termination_key(end: End) -> tuple[str, int]:
    return (end.object_type, int(end.object_id or 0))


def _termination(end: End) -> dict[str, Any]:
    return {"object_type": end.object_type, "object_id": end.object_id}


def _distinct(ends: Any) -> list[End]:
    """One `End` per resolved termination, in order of first appearance (Requirement 9.16)."""
    seen: dict[tuple[str, int], End] = {}
    for end in ends:
        seen.setdefault(_termination_key(end), end)
    return list(seen.values())


def _cable_label(sides: dict[str, list[End]]) -> str:
    def names(ends: list[End]) -> str:
        return " + ".join(f"{end.name}:{end.port}" for end in ends)

    return f"{names(sides['a'])} ↔ {names(sides['b'])}"


def _join(lines: tuple[int, ...]) -> str:
    if len(lines) == 1:
        return str(lines[0])
    return ", ".join(str(line) for line in lines[:-1]) + f" and {lines[-1]}"


class CablingImporter(BaseImporter):
    name = "cabling"
    endpoint_order = (CABLES,)

    choice_sources: ClassVar[dict[str, tuple[str, str]]] = {
        "cable_type": (CABLES, "type"),
        "cable_status": (CABLES, "status"),
        "length_unit": (CABLES, "length_unit"),
        "cable_profile": (CABLES, "profile"),
    }
    """`*_end_kind`, `*_port_side`, and `cable_group` are absent on purpose: they are this tool's
    own vocabulary for reading the sheet, not NetBox fields, so they validate against `columns.py`
    (Requirements 9.1, 9.15)."""

    # --- S2 ---------------------------------------------------------------------------------

    def extra_cell_checks(self, row: Row, lookups: Lookups) -> list[RowError]:
        errors: list[RowError] = []
        specs = self.specs()

        # Requirement 9.14: cable profiles arrived in NetBox 4.5 and the supported range starts at
        # 4.6, so an instance reporting no profile choices means the field has gone rather than not
        # yet arrived. Either way the value cannot be written, and dropping it silently would create
        # an unprofiled cable while telling the operator their breakout was recorded.
        profile = row.get("cable_profile").strip()
        if profile and not lookups.choices_for(CABLES, "profile"):
            errors.append(
                RowError(
                    Code.CABLE_PROFILE_UNSUPPORTED,
                    column="cable_profile",
                    value=profile,
                    expected="a blank cable_profile on this NetBox",
                    message=(
                        f"cable_profile {profile!r} cannot be recorded: this NetBox reports no "
                        f"cable profile choices at all. Leave the column blank, or point the tool "
                        f"at an instance that supports profiles."
                    ),
                )
            )

        # Requirement 9.12: NetBox stores a length as a number *and* a unit and rejects a length
        # without one, so a sheet carrying only the number would fail at apply rather than here.
        length = row.get("length").strip()
        if length and not row.get("length_unit").strip():
            errors.append(
                RowError(
                    Code.LENGTH_UNIT_MISSING,
                    column="length_unit",
                    value="(blank)",
                    expected="a length_unit whenever length is supplied",
                    message=(
                        f"length {length!r} was supplied without a length_unit. NetBox cannot "
                        f"record a length without its unit — one of: "
                        f"{', '.join(specs['length_unit'].choices)}."
                    ),
                )
            )

        color = row.get("color").strip()
        if color and not COLOR_PATTERN.fullmatch(color):
            errors.append(
                RowError(
                    Code.COLOR_MALFORMED,
                    column="color",
                    value=color,
                    expected="six hexadecimal digits with no leading '#'",
                    message=(
                        f"color {color!r} is not a six-digit hex value. NetBox stores a colour as "
                        f"six hex digits without the leading hash, for example 2196f3."
                    ),
                )
            )
        return errors

    # --- grouping (Requirement 9.15) --------------------------------------------------------

    def group_key(self, row: Row) -> Any:
        """The cable this row belongs to.

        A blank tag keys on the line, so an untagged row is a group of one and behaves exactly as
        every row did before this column existed. The tag is folded, so `BRK-01` and `brk-01` are
        the same cable.
        """
        tag = fold(row.get("cable_group"))
        return ("group", tag) if tag else ("line", row.line)

    def normalise(self, rows: list[Row]) -> list[Row]:
        """Give every row of a group the cable's own values (Requirement 9.18).

        A four-lane breakout states its type and profile once, on whichever row supplies them, and
        the other three leave those cells blank. Filling them in here rather than at planning time
        is what makes that legal: `cable_type` is a required column, so S2 would reject a blank cell
        before any later stage could go looking for the group's value.

        Only blank cells are filled, so two rows that genuinely disagree still disagree and S3
        still rejects them.
        """
        filled: list[Row] = []
        for group in self._grouped(rows):
            if len(group) == 1:
                filled.extend(group)
                continue
            shared = {key: self._group_value(group, key) for key in CABLE_COLUMNS}
            for row in group:
                values = dict(row.values)
                for key, value in shared.items():
                    if value and not values.get(key, "").strip():
                        values[key] = value
                filled.append(Row(line=row.line, values=values))
        return sorted(filled, key=lambda row: row.line)

    @staticmethod
    def _group_value(rows: list[Row], key: str) -> str:
        """A cable-level value, taken from the first row that supplies one (Requirement 9.18).

        S3 has already rejected the group if two rows supply different values, so the first
        non-blank is the group's only opinion, not a choice between candidates.
        """
        for row in rows:
            value = row.get(key).strip()
            if value:
                return value
        return ""

    # --- S3 ---------------------------------------------------------------------------------

    def check_consistency(self, rows: list[Row], collector: IssueCollector) -> None:
        seen: dict[tuple[str, str, str], tuple[int, str, Any]] = {}

        for row in rows:
            ends = self._ends(row)
            a_end, b_end = ends
            group = self.group_key(row)

            # Requirement 9.10: a cable between a port and itself is not a cable. Still true inside
            # a group — a lane that starts and ends on the same port is not a lane (9.17).
            if a_end.signature == b_end.signature:
                collector.add(
                    Severity.ERROR,
                    Code.SAME_PORT_BOTH_ENDS,
                    line=row.line,
                    column=b_end.port_column,
                    value=b_end.port,
                    expected="two different terminations",
                    message=(
                        f"Both ends of this row terminate on {a_end.description}. A cable has to "
                        f"connect two different ports."
                    ),
                )

            for end in ends:
                earlier = seen.get(end.signature)
                if earlier is None or earlier[0] == row.line:
                    seen.setdefault(end.signature, (row.line, end.description, group))
                    continue
                if earlier[2] == group:
                    # Requirement 9.17: within one group a repeated termination is the connector
                    # the rows share — the whole mechanism a breakout is written with.
                    continue
                # Requirement 9.11: a termination carries one cable, so the later row loses.
                collector.add(
                    Severity.ERROR,
                    Code.DUPLICATE_TERMINATION_IN_FILE,
                    line=row.line,
                    column=end.port_column,
                    value=end.port,
                    expected="a termination no other row in this file uses",
                    message=(
                        f"The {end.label}, {end.description}, is already cabled by line "
                        f"{earlier[0]} of this file. A termination can carry only one cable, so "
                        f"this row was rejected. If these two rows are one breakout cable, give "
                        f"them both the same cable_group."
                    ),
                    related_line=earlier[0],
                )

        for group in self._grouped(rows):
            self._check_group(group, collector)

    def _check_group(self, rows: list[Row], collector: IssueCollector) -> None:
        """The rules that only mean anything once several rows describe one cable."""
        self._check_group_agrees(rows, collector)

        a_count = len({end.signature for end in (read_end(row, "a") for row in rows)})
        b_count = len({end.signature for end in (read_end(row, "b") for row in rows)})
        counts = {"a": a_count, "b": b_count}
        profile = self._group_value(rows, "cable_profile")
        lines = tuple(row.line for row in rows)
        last = rows[-1].line

        if not profile:
            if a_count > 1 or b_count > 1:
                # Requirement 9.20: NetBox will store this cable, but with no profile it cannot say
                # which lane reaches which far end — which is the reason to record a breakout.
                collector.add(
                    Severity.WARNING,
                    Code.CABLE_GROUP_WITHOUT_PROFILE,
                    line=rows[0].line,
                    column="cable_profile",
                    value="(blank)",
                    expected="a cable_profile describing how this cable breaks out",
                    message=(
                        f"Lines {_join(lines)} are one cable with {a_count} A-side and {b_count} "
                        f"B-side termination(s), but no cable_profile. NetBox will record the "
                        f"cable and will not be able to trace which lane reaches which end."
                    ),
                )
            return

        shape = profile_structure(profile)
        if shape is None:
            # Requirement 9.19: a value NetBox accepts whose shape this tool cannot read. Warn and
            # let NetBox be the judge, rather than inventing a limit and blocking a legal cable.
            collector.add(
                Severity.WARNING,
                Code.PROFILE_STRUCTURE_UNKNOWN,
                line=rows[0].line,
                column="cable_profile",
                value=profile,
                expected="a profile whose connector counts this tool can read",
                message=(
                    f"cable_profile {profile!r} is accepted by NetBox, but this tool cannot read "
                    f"how many connectors it has per side, so it could not check the number of "
                    f"terminations. NetBox will validate that itself when the cable is created."
                ),
            )
            return

        for side in SIDES:
            limit = shape.connectors(side)
            if counts[side] <= limit:
                continue
            collector.add(
                Severity.ERROR,
                Code.PROFILE_CONNECTOR_OVERFLOW,
                line=last,
                column=f"{side}_port",
                value=str(counts[side]),
                expected=f"at most {limit} distinct {side.upper()}-side termination(s)",
                message=(
                    f"Lines {_join(lines)} are one cable with profile {profile!r}, which has "
                    f"{shape.describe(side)} on the {side.upper()} side — so at most {limit} "
                    f"termination(s) there. This group names {counts[side]}. Either split the "
                    f"group or choose a profile with more connectors."
                ),
                related_line=rows[0].line,
            )

    def _check_group_agrees(self, rows: list[Row], collector: IssueCollector) -> None:
        """Requirement 9.18: one cable has one type, one profile, and one label."""
        if len(rows) < 2:
            return
        for key in CABLE_COLUMNS:
            first_line, first_value = 0, ""
            for row in rows:
                value = row.get(key).strip()
                if not value:
                    continue
                if not first_value:
                    first_line, first_value = row.line, value
                    continue
                if fold(value) == fold(first_value):
                    continue
                collector.add(
                    Severity.ERROR,
                    Code.CABLE_GROUP_FIELD_CONFLICT,
                    line=row.line,
                    column=key,
                    value=value,
                    expected=f"{first_value!r}, or a blank cell",
                    message=(
                        f"Line {row.line} says {key} is {value!r} while line {first_line} of the "
                        f"same cable_group says {first_value!r}. These rows are one cable, which "
                        f"has one {key}: correct one of them, or leave this cell blank to take the "
                        f"value from line {first_line}."
                    ),
                    related_line=first_line,
                )

    # --- S4 ---------------------------------------------------------------------------------

    def prefetch(self, rows: list[Row], lookups: Lookups) -> None:
        """Every device by name in one pass, then every one of their ports (Requirement 15.2)."""
        names: list[str] = []
        kinds: set[str] = set()
        for row in rows:
            for end in self._ends(row):
                names.append(end.name)
                kinds.add(end.termination_kind)

        lookups.prefetch(DEVICES, names=names)
        device_ids = [
            int(record["id"]) for record in lookups.all_records(DEVICES) if record.get("id")
        ]
        if device_ids and kinds:
            lookups.prefetch_terminations(device_ids, tuple(sorted(kinds)))

    def plan_row(
        self,
        row: Row,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
    ) -> PlannedRow:
        """One row is a group of one, so the group path is the only path (Requirement 9.15)."""
        return self.plan_group([row], lookups, registry, state, collector)[0]

    def plan_group(
        self,
        rows: list[Row],
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
    ) -> list[PlannedRow]:
        planned = [PlannedRow(line=row.line) for row in rows]
        resolved: list[tuple[End, End]] = []

        for row, row_plan in zip(rows, planned, strict=True):
            ends = tuple(
                self._resolve(end, row, lookups, registry, state, collector, row_plan)
                for end in self._ends(row)
            )
            resolved.append(ends)

        if any(row_plan.outcome == RowOutcome.BLOCKED for row_plan in planned):
            # The group is one cable: if one row's port cannot be found, the cable cannot be
            # created as described, so every row of it is blocked rather than half of it planned.
            for row_plan in planned:
                row_plan.outcome = RowOutcome.BLOCKED
            return planned

        sides = {
            "a": _distinct(ends[0] for ends in resolved),
            "b": _distinct(ends[1] for ends in resolved),
        }
        self._describe_connectors(planned, resolved, sides)

        occupancy = self._occupancy(sides, lookups)
        if occupancy.blocks:
            self._report_partly_cabled(rows, planned, occupancy, collector)
            return planned
        if occupancy.taken:
            self._report_already_cabled(planned, occupancy, lookups, collector)
            return planned

        planned[0].actions.append(self._cable(rows, sides))
        for row_plan in planned:
            row_plan.outcome = RowOutcome.CREATE
        return planned

    @staticmethod
    def _describe_connectors(
        planned: list[PlannedRow],
        resolved: list[tuple[End, End]],
        sides: dict[str, list[End]],
    ) -> None:
        """Requirement 9.21: show the connector each row's termination will be given.

        NetBox assigns connectors from the order of the terminations array and exposes them
        read-only, so row order *is* lane order and the operator has no other way to see it. Naming
        it in the preview makes a re-sorted sheet visible before consent rather than after.

        Position is looked up by resolved termination rather than by comparing `End`s, so two rows
        spelling one port differently — `swp1` and `SWP1` — still land on the same connector.
        """
        order = {
            side: {_termination_key(end): index for index, end in enumerate(ends)}
            for side, ends in sides.items()
        }
        for row_plan, ends in zip(planned, resolved, strict=True):
            parts = [
                f"{side.upper()} connector {order[side][_termination_key(end)] + 1} "
                f"of {len(sides[side])}"
                for side, end in zip(SIDES, ends, strict=True)
                if len(sides[side]) > 1
            ]
            if parts:
                row_plan.resolved["connector"] = ", ".join(parts)

    # --- one end at a time ------------------------------------------------------------------

    @staticmethod
    def _ends(row: Row) -> tuple[End, End]:
        return read_end(row, "a"), read_end(row, "b")

    def _resolve(
        self,
        end: End,
        row: Row,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> End:
        # Requirement 9.4 asks for *exactly one* match, so the prefetched records are counted
        # rather than asking the cache for a single winner: a NetBox holding two devices of the
        # same name is ambiguous, not resolved.
        matches = [
            record
            for record in lookups.all_records(DEVICES)
            if fold(record.get("name")) == fold(end.name)
        ]
        if len(matches) != 1:
            if not matches:
                message = (
                    f"The {end.label} names {end.name!r}, which no device or panel in NetBox is "
                    f"called. This importer creates neither: add it in NetBox first."
                )
            else:
                message = (
                    f"The {end.label} names {end.name!r}, which {len(matches)} devices in NetBox "
                    f"share, so the end is ambiguous and no cable can be placed."
                )
            collector.add(
                Severity.ERROR,
                Code.END_NOT_FOUND,
                line=row.line,
                column=end.end_column,
                value=end.name,
                expected="the name of exactly one existing device or panel",
                message=message,
            )
            planned.outcome = RowOutcome.BLOCKED
            return end

        device = matches[0]
        end.device = device
        device_name = str(device.get("name") or end.name)

        ports = lookups.terminations_for(int(device["id"]), end.termination_kind)
        termination = ports.get(fold(end.port))
        if termination is None:
            # Requirement 9.5: say which side and which port name, because a panel row has four
            # port columns and "not found" alone does not point at one of them.
            hint = (
                f" If you meant the other face, set {end.side}_port_side."
                if end.kind == PANEL
                else ""
            )
            collector.add(
                Severity.ERROR,
                Code.PORT_NOT_FOUND,
                line=row.line,
                column=end.port_column,
                value=end.port,
                expected=f"the name of an existing {end.port_label} on {device_name!r}",
                message=(
                    f"The {end.label} port {end.port!r} is not a {end.port_label} on "
                    f"{device_name!r}.{hint}"
                ),
                related_object=f"Device {device_name}",
                related_url=self._url(lookups, DEVICES, device),
            )
            planned.outcome = RowOutcome.BLOCKED
            return end

        end.termination = termination
        planned.resolved[end.end_column] = f"{device_name} (existing)"
        planned.resolved[end.port_column] = (
            f"{termination.get('name')} ({end.port_label}, existing)"
        )
        self._register(state, registry, end, lookups)
        return end

    def _register(
        self,
        state: dict[str, str],
        registry: Registry,
        end: End,
        lookups: Lookups,
    ) -> None:
        """Record the ids and the state the plan depends on (Requirement 10.14).

        The port's occupancy is part of that state: a port cabled by someone else between
        validation and apply changes nothing this importer would report as an error, so only the
        signature catches it and aborts before the first write.
        """
        device_id = int(end.device["id"])
        registry.remember(DEVICES, Registry.key(end.name), device_id)
        state[f"{DEVICES}:{fold(end.name)}"] = f"id={device_id}"

        endpoint = TERMINATION_ENDPOINTS[end.termination_kind]
        cable = lookups.occupied(end.object_type, end.object_id)
        state[f"{endpoint}:{end.object_id}"] = (
            f"device={device_id},cable={cable.get('id') if cable else 'none'}"
        )

    # --- occupancy (Requirements 9.9, 9.22) -------------------------------------------------

    def _occupancy(self, sides: dict[str, list[End]], lookups: Lookups) -> "Occupancy":
        """Which of a cable's terminations are free, and what holds the rest."""
        found = Occupancy(multi=any(len(ends) > 1 for ends in sides.values()))
        for ends in sides.values():
            for end in ends:
                cable = lookups.occupied(end.object_type, end.object_id)
                if cable is None:
                    found.free.append(end)
                    continue
                found.taken.append((end, cable))
                found.holders.setdefault(int(cable.get("id") or 0), cable)
        return found

    def _report_already_cabled(
        self,
        planned: list[PlannedRow],
        occupancy: "Occupancy",
        lookups: Lookups,
        collector: IssueCollector,
    ) -> None:
        """Requirement 9.9: an occupied termination skips and names the cable holding it."""
        for end, cable in occupancy.taken:
            held = describe_cable(cable)
            collector.add(
                Severity.INFO,
                Code.PORT_ALREADY_CABLED,
                line=end.line,
                column=end.port_column,
                value=end.port,
                expected="a termination with no cable on it",
                message=(
                    f"The {end.label}, {end.description}, already carries {held}. "
                    f"{'The group was' if occupancy.multi else 'The row was'} skipped: this "
                    f"iteration never modifies or removes an existing cable."
                ),
                related_object=held,
                related_url=lookups.client.object_url(CABLES, int(cable["id"]))
                if cable.get("id")
                else "",
            )
        name = describe_cable(next(iter(occupancy.holders.values())))
        for row_plan in planned:
            row_plan.outcome = RowOutcome.SKIP_EXISTS
            row_plan.resolved["object"] = name

    def _report_partly_cabled(
        self,
        rows: list[Row],
        planned: list[PlannedRow],
        occupancy: "Occupancy",
        collector: IssueCollector,
    ) -> None:
        """Requirement 9.22: a half-built breakout is a decision, not a skip.

        Skipping would silently drop the lanes that are missing; creating would need a second cable
        on a termination that already has one; completing the existing cable would be a PATCH, and
        this iteration modifies nothing it did not create (Requirement 11.3). So it is reported,
        with both halves named, for a human to finish in NetBox.
        """
        held = ", ".join(describe_cable(cable) for cable in occupancy.holders.values())
        free = ", ".join(end.description for end in occupancy.free) or "none"
        lines = tuple(row.line for row in rows)
        collector.add(
            Severity.ERROR,
            Code.CABLE_GROUP_PARTLY_CABLED,
            line=rows[0].line,
            column="cable_group",
            value=rows[0].get("cable_group").strip(),
            expected="a cable whose terminations are all free, or all already on one cable",
            message=(
                f"Lines {_join(lines)} are one cable, and it is already partly in place: "
                f"{held} holds some of its terminations while these are still free: {free}. This "
                f"iteration only creates, so it cannot add the missing termination(s) to an "
                f"existing cable. Finish or remove that cable in NetBox, then re-upload."
            ),
        )
        for row_plan in planned:
            row_plan.outcome = RowOutcome.BLOCKED

    def _cable(self, rows: list[Row], sides: dict[str, list[End]]) -> CreateAction:
        payload: dict[str, Any] = {
            # Requirement 9.6: terminations are arrays on the cable itself, and 9.16: a group
            # contributes its distinct terminations in row order. The cable-terminations endpoint
            # is read-only and is never written to.
            "a_terminations": [_termination(end) for end in sides["a"]],
            "b_terminations": [_termination(end) for end in sides["b"]],
            "type": fold(self._group_value(rows, "cable_type")),
            # Requirement 9.12.
            "status": fold(self._group_value(rows, "cable_status")) or DEFAULT_STATUS,
        }

        # Requirement 9.13: blank leaves `profile` out of the payload entirely, so an unprofiled
        # cable is posted exactly as it was before this column existed.
        profile = self._group_value(rows, "cable_profile")
        if profile:
            payload["profile"] = fold(profile)
        label = self._group_value(rows, "label")
        if label:
            payload["label"] = label
        length = self._group_value(rows, "length")
        if length:
            payload["length"] = int(length)
            payload["length_unit"] = fold(self._group_value(rows, "length_unit"))
        color = self._group_value(rows, "color")
        if color:
            payload["color"] = fold(color)

        lines = tuple(row.line for row in rows)
        return CreateAction(
            endpoint=CABLES,
            payload=payload,
            key=Registry.key(
                *(
                    part
                    for end in sides["a"] + sides["b"]
                    for part in (end.object_type, end.object_id)
                )
            ),
            label=_cable_label(sides),
            line=lines[0],
            lines=lines,
            object_kind="cable",
        )

    @staticmethod
    def _url(lookups: Lookups, endpoint: str, existing: dict[str, Any]) -> str:
        object_id = existing.get("id")
        return lookups.client.object_url(endpoint, int(object_id)) if object_id else ""
