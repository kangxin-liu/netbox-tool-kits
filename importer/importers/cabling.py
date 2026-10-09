# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Sheet 3 — one row per cable, between devices and patch panels (Requirement 9).

A cable is created by POSTing `a_terminations` and `b_terminations` to `dcim.cables` as arrays of
`{object_type, object_id}` (Requirement 9.6). NetBox's cable-terminations endpoint is read-only, so
nothing here writes to it.

Which port model an end resolves against is decided by `*_end_kind`, and for a panel by
`*_port_side`: a device end is an interface, a panel end is a front port unless the side says rear
(Requirements 9.1–9.3). A panel is an ordinary NetBox device, so both kinds are found the same way
and only the port model differs.

Transceivers are deferred to iteration 2 (decision D3). No column here reads or writes optic
information, and the `type` of an interface this importer did not create is never touched
(Requirement 9.8). A `transceiver` column left in an uploaded file is simply an unrecognised column,
which the parser reports as informational and drops (Requirement 5.6).

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

**Creating a missing interface (D12).** A device-end port name that resolves to nothing used to be
the end of the row. Behind `NETBOX_AUTOCREATE_INTERFACES` it is now something the sheet can fix,
from the device type's interface template where one of that name exists and from `*_interface_type`
otherwise (Requirements 9.25, 9.26). The template is tried first and that order is the safety
property, not a preference: it invents nothing, and a mistyped `swp1o` matches no template either,
so it falls through to a branch that demands an explicit type rather than becoming a port quietly.

**Breakout children (D13).** `a_parent` / `b_parent` decouple a row's lane from the physical cable
termination. A value such as `spine01:swp1` makes `swp1` the cabled termination while the row's
`swp1s0` port is an uncabled `virtual` child with `parent=swp1`. Both ends are symmetric, so one
free-text `cable_group` can describe a shuffle with several physical parents per side. Distinct
terminations, profile limits, duplicate checks, and occupancy all use parents; the preview retains
the child-to-child lane map (Requirements 9.34–9.43).

That costs this sheet the property it used to have. `endpoint_order` was one entry, so the whole
file was a single transactional batch with no half-applied window at all. D12 made it interfaces
then cables; D13 may make it physical parents, virtual children, then cables. A later batch failure
leaves earlier interfaces behind, and re-uploading clears the residue because they are then found
rather than planned (Requirement 9.32). Panel ports are never created: a front port is defined by
the rear port and position it maps to, and no column here says either (9.29).
"""

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

from django.conf import settings

from importer.cable_profiles import structure as profile_structure
from importer.device_addresses import choice_value, related_id
from importer.importers.base import (
    BaseImporter,
    CreateAction,
    PlannedRow,
    Ref,
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
INTERFACES = "dcim.interfaces"
INTERFACE_TEMPLATES = "dcim.interface-templates"

DEVICE = "device"
PANEL = "panel"
FRONT = "front"
REAR = "rear"

DEFAULT_END_KIND = DEVICE
DEFAULT_PORT_SIDE = FRONT
DEFAULT_STATUS = "connected"
VIRTUAL = "virtual"

INTERFACE_PHASE = "interfaces"
CHILD_PHASE = "children"
CABLE_PHASE = "cables"

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

# Template fields copied onto an interface created from one (Requirement 9.25). Choice fields come
# back from NetBox as `{value, label}` dicts and go out as the bare value; the rest are copied as
# they are. Task 31a is what pins this list down against a live 4.6.8 — until it runs, a field
# NetBox names differently is copied as nothing rather than wrongly, because only keys the template
# actually carries are read.
TEMPLATE_CHOICE_FIELDS = ("type", "duplex", "poe_mode", "poe_type", "rf_role")
TEMPLATE_PLAIN_FIELDS = ("label", "description", "speed", "mgmt_only")

FROM_TEMPLATE = "the device type"
FROM_SHEET = "the sheet"


@dataclass(frozen=True)
class PlannedInterface:
    """An interface this run will create, and where its type came from (Requirements 9.25, 9.26)."""

    device_id: int
    device_name: str
    name: str
    type_value: str
    source: str
    payload: dict[str, Any]
    phase: str = INTERFACE_PHASE

    @property
    def key(self) -> tuple[Any, ...]:
        return Registry.key(self.device_name, self.name)

    @property
    def label(self) -> str:
        return f"{self.device_name} {self.name}"


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
    parent_value: str = ""
    parent_device: str = ""
    parent_port: str = ""
    line: int = 0
    """The spreadsheet line this end was read from.

    Carried on the end rather than looked up again later, so an issue about one termination of a
    four-row breakout points at the row that named it (Requirement 16.2).
    """

    device: dict[str, Any] | None = None
    termination: dict[str, Any] | None = None
    planned: PlannedInterface | None = None
    """Set instead of `termination` when this port does not exist yet and D12 will create it."""

    child: dict[str, Any] | None = None
    """Existing virtual child named by `port` when D13 declares a parent."""

    planned_child: PlannedInterface | None = None
    """Virtual child this run will create under the cable-terminating parent."""

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
    def type_column(self) -> str:
        return f"{self.side}_interface_type"

    @property
    def parent_column(self) -> str:
        return f"{self.side}_parent"

    @property
    def has_parent(self) -> bool:
        return bool(self.parent_port)

    @property
    def cable_port(self) -> str:
        """The interface the physical cable terminates on (Requirement 9.36)."""
        return self.parent_port or self.port

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
        return (self.object_type, fold(self.name), fold(self.cable_port))

    @property
    def description(self) -> str:
        return f"{self.port_label} {self.cable_port!r} on {self.name!r}"

    @property
    def lane_description(self) -> str:
        if not self.has_parent:
            return self.description
        return f"virtual child {self.port!r} under interface {self.parent_port!r} on {self.name!r}"

    @property
    def object_id(self) -> int | None:
        """The id of an existing termination. `None` while it is only planned."""
        return int(self.termination["id"]) if self.termination else None

    @property
    def termination_value(self) -> Any:
        """What goes in the cable payload: an id, or a `Ref` to the interface being created.

        `resolved_payload` swaps the `Ref` for the real id once the interface batch has returned,
        which is why the interface endpoint has to come first in `endpoint_order` (Req 9.32).
        """
        if self.planned is not None:
            return Ref(INTERFACES, self.planned.key)
        return self.object_id


def read_end(row: Row, side: str) -> End:
    """One end of a row with the documented defaults applied (Requirements 9.1, 9.3).

    `*_end_kind` is declared required in `columns.py`, so in practice a blank cell is rejected at
    S2 before the `device` default below is ever reached. The default is implemented anyway because
    9.1 specifies it: relaxing the column to optional is then the only change needed.
    """
    kind = fold(row.get(f"{side}_end_kind")) or DEFAULT_END_KIND
    port_side = fold(row.get(f"{side}_port_side")) or DEFAULT_PORT_SIDE
    object_type, termination_kind = PORT_MODELS[(kind, port_side)]
    parent_value = row.get(f"{side}_parent").strip()
    parent_device = ""
    parent_port = ""
    if parent_value.count(":") == 1:
        parent_device, parent_port = (part.strip() for part in parent_value.split(":", 1))
    return End(
        side=side,
        name=row.get(f"{side}_end").strip(),
        kind=kind,
        port=row.get(f"{side}_port").strip(),
        port_side=port_side,
        object_type=object_type,
        termination_kind=termination_kind,
        parent_value=parent_value,
        parent_device=parent_device,
        parent_port=parent_port,
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


def _termination_key(end: End) -> tuple[str, Any]:
    """What makes two resolved ends the same termination.

    A planned interface has no id yet, so it is keyed on device and port name instead. The `new:`
    prefix keeps it from ever colliding with an id, and makes a cable's registry key readable when
    one of its terminations is being created in the same run.
    """
    if end.termination is not None:
        return (end.object_type, int(end.termination["id"]))
    return (end.object_type, f"new:{fold(end.name)}/{fold(end.cable_port)}")


def _termination(end: End) -> dict[str, Any]:
    return {"object_type": end.object_type, "object_id": end.termination_value}


def _distinct(ends: Any) -> list[End]:
    """One `End` per resolved termination, in order of first appearance (Requirement 9.16)."""
    seen: dict[tuple[str, int], End] = {}
    for end in ends:
        seen.setdefault(_termination_key(end), end)
    return list(seen.values())


def _cable_label(sides: dict[str, list[End]]) -> str:
    def names(ends: list[End]) -> str:
        return " + ".join(f"{end.name}:{end.cable_port}" for end in ends)

    return f"{names(sides['a'])} ↔ {names(sides['b'])}"


def _join(lines: tuple[int, ...]) -> str:
    if len(lines) == 1:
        return str(lines[0])
    return ", ".join(str(line) for line in lines[:-1]) + f" and {lines[-1]}"


class CablingImporter(BaseImporter):
    name = "cabling"
    # Interfaces before cables, because a cable may terminate on one this run is creating
    # (Requirement 9.32). With the flag off nothing is ever planned for the first endpoint and the
    # apply is the single batch it always was.
    endpoint_order = (INTERFACES, CABLES)
    # D13 can create a physical parent and a virtual child on the same endpoint. The child payload
    # needs the id returned for the parent, so they are two transactional interface batches before
    # the cable batch. Rows without children still use the D12 two-batch path.
    apply_phases = (
        (INTERFACES, "post", INTERFACE_PHASE),
        (INTERFACES, "post", CHILD_PHASE),
        (CABLES, "post", CABLE_PHASE),
    )

    choice_sources: ClassVar[dict[str, tuple[str, str]]] = {
        "cable_type": (CABLES, "type"),
        "cable_status": (CABLES, "status"),
        "length_unit": (CABLES, "length_unit"),
        "cable_profile": (CABLES, "profile"),
        # Validated in either flag state, so a bad value is caught on the sheet that will one day be
        # applied with autocreate on rather than on the run that needs it (Requirement 9.27).
        "a_interface_type": (INTERFACES, "type"),
        "b_interface_type": (INTERFACES, "type"),
    }
    """`*_end_kind`, `*_port_side`, and `cable_group` are absent on purpose: they are this tool's
    own vocabulary for reading the sheet, not NetBox fields, so they validate against `columns.py`
    (Requirements 9.1, 9.15)."""

    def __init__(self) -> None:
        self._templates: dict[tuple[int, str], dict[str, Any]] = {}
        self._planned: dict[tuple[int, str], PlannedInterface] = {}
        """One entry per interface this run will create, so four rows naming the same missing port
        create it once and warn about it once (Requirement 9.30). Reset by `prefetch`, which runs
        once per validation — including the pre-apply re-validation of Requirement 10.14."""

    # --- S2 ---------------------------------------------------------------------------------

    def extra_cell_checks(self, row: Row, lookups: Lookups) -> list[RowError]:
        errors: list[RowError] = []
        specs = self.specs()

        for side in SIDES:
            column = f"{side}_parent"
            value = row.get(column).strip()
            if not value:
                continue
            parts = [part.strip() for part in value.split(":")]
            if len(parts) != 2 or not all(parts):
                errors.append(
                    RowError(
                        Code.BREAKOUT_PARENT_MALFORMED,
                        column=column,
                        value=value,
                        expected="{device}:{interface}, for example spine01:swp1",
                        message=(
                            f"{column} {value!r} is not a breakout parent. Use exactly "
                            f"{{device}}:{{interface}} with both names present."
                        ),
                    )
                )

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
        child_parents: dict[tuple[str, str], tuple[int, str]] = {}

        for row in rows:
            ends = self._ends(row)
            a_end, b_end = ends
            group = self.group_key(row)

            for end in ends:
                if not end.has_parent:
                    continue
                if end.kind != DEVICE:
                    collector.add(
                        Severity.ERROR,
                        Code.BREAKOUT_PARENT_MISMATCH,
                        line=row.line,
                        column=end.parent_column,
                        value=end.parent_value,
                        expected="a parent only on a device end",
                        message=(
                            f"{end.parent_column} declares interface {end.parent_port!r}, but "
                            f"{end.end_column} is a panel. Breakout parents are device interfaces."
                        ),
                    )
                    continue
                if fold(end.parent_device) != fold(end.name):
                    collector.add(
                        Severity.ERROR,
                        Code.BREAKOUT_PARENT_MISMATCH,
                        line=row.line,
                        column=end.parent_column,
                        value=end.parent_value,
                        expected=f"{end.name}:{end.parent_port}",
                        message=(
                            f"{end.parent_column} names device {end.parent_device!r}, but "
                            f"{end.end_column} names {end.name!r}. A child and its parent must be "
                            f"on the same device."
                        ),
                    )

                child_key = (fold(end.name), fold(end.port))
                earlier_parent = child_parents.get(child_key)
                if earlier_parent is None:
                    child_parents[child_key] = (row.line, end.parent_port)
                elif fold(earlier_parent[1]) != fold(end.parent_port):
                    collector.add(
                        Severity.ERROR,
                        Code.CHILD_PARENT_CONFLICT,
                        line=row.line,
                        column=end.parent_column,
                        value=end.parent_value,
                        expected=f"{end.name}:{earlier_parent[1]}",
                        message=(
                            f"Line {row.line} places child {end.name}:{end.port} under "
                            f"{end.parent_port!r}, while line {earlier_parent[0]} places it under "
                            f"{earlier_parent[1]!r}. One child interface has one parent."
                        ),
                        related_line=earlier_parent[0],
                    )

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

        if settings.NETBOX_AUTOCREATE_INTERFACES:
            self._check_interface_types(rows, collector)

    def _check_interface_types(self, rows: list[Row], collector: IssueCollector) -> None:
        """Requirement 9.31: one interface has one type, whoever names it.

        Judged here rather than at S4 because it is a contradiction in the file itself — the sheet
        says one port is two different things — and that is true whether or not the port turns out
        to exist. Only meaningful while autocreate is on, since the cell is otherwise never read.
        """
        seen: dict[tuple[str, str], tuple[int, str]] = {}
        for row in rows:
            for end in self._ends(row):
                if end.kind == PANEL:
                    continue
                value = row.get(end.type_column).strip()
                if not value:
                    continue
                # D13's type cell describes the physical parent, not the fixed-type virtual child.
                key = (fold(end.name), fold(end.cable_port))
                earlier = seen.get(key)
                if earlier is None:
                    seen[key] = (row.line, value)
                    continue
                first_line, first_value = earlier
                if fold(first_value) == fold(value) or first_line == row.line:
                    continue
                collector.add(
                    Severity.ERROR,
                    Code.INTERFACE_TYPE_CONFLICT,
                    line=row.line,
                    column=end.type_column,
                    value=value,
                    expected=f"{first_value!r}, or a blank cell",
                    message=(
                        f"Line {row.line} says {end.description} is a {value!r} interface while "
                        f"line {first_line} says {first_value!r}. One interface has one type: "
                        f"correct one of them, or leave this cell blank."
                    ),
                    related_line=first_line,
                )

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

        self._templates = {}
        self._planned = {}
        if not settings.NETBOX_AUTOCREATE_INTERFACES:
            # Not merely a short circuit: with the flag off this importer issues exactly the
            # requests it always did, so the request budget of Requirement 15.2 is unchanged for
            # every sheet in use today.
            return

        type_ids = sorted(
            {
                type_id
                for record in lookups.all_records(DEVICES)
                if (type_id := related_id(record.get("device_type"))) is not None
            }
        )
        if type_ids:
            lookups.prefetch(INTERFACE_TEMPLATES, device_type_id=type_ids)
        for template in lookups.all_records(INTERFACE_TEMPLATES):
            type_id = related_id(template.get("device_type"))
            if type_id is None:
                continue
            self._templates.setdefault((type_id, fold(template.get("name"))), template)

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
        interface_actions = self._interface_actions(resolved)
        if occupancy.taken:
            self._report_already_cabled(planned, occupancy, lookups, collector)
            if interface_actions:
                # The physical cable already exists, but D13 may still need to materialise the
                # virtual lane inventory. The cable is skipped; only the missing interfaces apply.
                planned[0].actions.extend(interface_actions)
                for row_plan in planned:
                    row_plan.outcome = RowOutcome.CREATE
            return planned

        planned[0].actions.extend(interface_actions)
        planned[0].actions.append(self._cable(rows, sides))
        for row_plan in planned:
            row_plan.outcome = RowOutcome.CREATE
        return planned

    @staticmethod
    def _interface_actions(resolved: list[tuple[End, End]]) -> list[CreateAction]:
        """The interfaces this cable needs that do not exist yet (Requirement 9.25).

        Two groups naming one missing interface each carry an action for it and apply writes it
        once, the same way twenty hierarchy rows in one new datahall create that datahall once:
        `_dedupe` keys on the registry key, and `object_count` counts distinct keys.
        """
        actions: list[CreateAction] = []
        seen: set[tuple[str, tuple[Any, ...]]] = set()
        for ends in resolved:
            for end in ends:
                for interface in (end.planned, end.planned_child):
                    if interface is None:
                        continue
                    identity = (interface.phase, interface.key)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    actions.append(
                        CreateAction(
                            endpoint=INTERFACES,
                            payload=dict(interface.payload),
                            key=interface.key,
                            label=interface.label,
                            line=end.line,
                            object_kind="interface",
                            phase=interface.phase,
                        )
                    )
        return actions

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
            if any(end.has_parent for end in ends):
                row_plan.resolved["lane_map"] = " ↔ ".join(
                    (
                        f"{end.name}:{end.port} (child of {end.cable_port})"
                        if end.has_parent
                        else f"{end.name}:{end.port}"
                    )
                    for end in ends
                )

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
        if end.has_parent:
            if not self._resolve_parented(end, row, device, device_name, ports, lookups, collector):
                planned.outcome = RowOutcome.BLOCKED
                return end
            planned.resolved[end.end_column] = f"{device_name} (existing)"
            planned.resolved[end.parent_column] = self._describe_parent(end)
            planned.resolved[end.port_column] = self._describe_child(end)
            self._register(state, registry, end, lookups)
            return end

        termination = ports.get(fold(end.port))
        if termination is not None:
            end.termination = termination
            self._note_type_ignored(end, row, device_name, collector)
        elif not self._plan_interface(end, row, device, device_name, lookups, collector):
            planned.outcome = RowOutcome.BLOCKED
            return end

        planned.resolved[end.end_column] = f"{device_name} (existing)"
        planned.resolved[end.port_column] = self._describe_port(end)
        self._register(state, registry, end, lookups)
        return end

    def _describe_port(self, end: End) -> str:
        """Requirement 9.33: a port that was found reads differently from one about to be made."""
        if end.planned is not None:
            return (
                f"{end.port} ({end.port_label}, will be created — "
                f"type {end.planned.type_value} from {end.planned.source})"
            )
        return f"{end.termination.get('name')} ({end.port_label}, existing)"

    def _describe_parent(self, end: End) -> str:
        if end.planned is not None:
            return (
                f"{end.name}:{end.parent_port} (physical interface, will be created — "
                f"type {end.planned.type_value} from {end.planned.source})"
            )
        return f"{end.name}:{end.termination.get('name')} (physical interface, existing)"

    @staticmethod
    def _describe_child(end: End) -> str:
        if end.planned_child is not None:
            return f"{end.port} (virtual child of {end.parent_port}, will be created)"
        return f"{end.child.get('name')} (virtual child of {end.parent_port}, existing)"

    def _resolve_parented(
        self,
        end: End,
        row: Row,
        device: dict[str, Any],
        device_name: str,
        ports: dict[str, dict[str, Any]],
        lookups: Lookups,
        collector: IssueCollector,
    ) -> bool:
        """Resolve/create a physical parent and its uncabled virtual child (D13)."""
        parent = ports.get(fold(end.parent_port))
        if parent is not None:
            end.termination = parent
            self._note_type_ignored(end, row, device_name, collector)
        elif not self._plan_interface(
            end,
            row,
            device,
            device_name,
            lookups,
            collector,
            target_name=end.parent_port,
            target_column=end.parent_column,
        ):
            return False

        child = ports.get(fold(end.port))
        if child is not None:
            expected_parent = end.object_id
            actual_parent = related_id(child.get("parent"))
            child_type = choice_value(child.get("type"))
            conflicts: list[str] = []
            if expected_parent is not None and actual_parent != expected_parent:
                conflicts.append(
                    f"parent id {actual_parent or 'none'} instead of {expected_parent}"
                )
            if fold(child_type) != VIRTUAL:
                conflicts.append(f"type {child_type or '(blank)'} instead of virtual")
            if conflicts:
                collector.add(
                    Severity.ERROR,
                    Code.CHILD_INTERFACE_CONFLICT,
                    line=row.line,
                    column=end.port_column,
                    value=end.port,
                    expected=f"a virtual child of {device_name}:{end.parent_port}",
                    message=(
                        f"{end.lane_description} already exists with "
                        f"{' and '.join(conflicts)}. This importer never reparents or re-types an "
                        f"existing interface; correct it in NetBox first."
                    ),
                    related_object=f"Device {device_name}",
                    related_url=self._url(lookups, DEVICES, device),
                )
                return False
            end.child = child
            return True

        if not settings.NETBOX_AUTOCREATE_INTERFACES:
            collector.add(
                Severity.ERROR,
                Code.PORT_NOT_FOUND,
                line=row.line,
                column=end.port_column,
                value=end.port,
                expected=f"a virtual child of {device_name}:{end.parent_port}",
                message=(
                    f"{end.lane_description} does not exist. Set "
                    f"NETBOX_AUTOCREATE_INTERFACES=true to create breakout children, or add it "
                    f"in NetBox first."
                ),
                related_object=f"Device {device_name}",
                related_url=self._url(lookups, DEVICES, device),
            )
            return False

        device_id = int(device["id"])
        already = self._planned.get((device_id, fold(end.port)))
        if already is not None:
            end.planned_child = already
            return True

        parent_value: int | Ref
        if end.planned is not None:
            parent_value = Ref(INTERFACES, end.planned.key)
        else:
            parent_value = int(end.termination["id"])
        child_plan = PlannedInterface(
            device_id=device_id,
            device_name=device_name,
            name=end.port,
            type_value=VIRTUAL,
            source=f"parent mapping {end.parent_value}",
            payload={
                "device": device_id,
                "name": end.port,
                "type": VIRTUAL,
                "parent": parent_value,
            },
            phase=CHILD_PHASE,
        )
        self._planned[(device_id, fold(end.port))] = child_plan
        end.planned_child = child_plan
        collector.add(
            Severity.WARNING,
            Code.INTERFACE_WILL_BE_CREATED,
            line=row.line,
            column=end.port_column,
            value=end.port,
            expected="an existing virtual child, or consent to creating this one",
            message=(
                f"{end.lane_description} does not exist and will be created as a {VIRTUAL!r} "
                f"interface. Check the child and parent spelling before applying."
            ),
            related_object=f"Device {device_name}",
        )
        return True

    def _note_type_ignored(
        self,
        end: End,
        row: Row,
        device_name: str,
        collector: IssueCollector,
    ) -> None:
        """Requirement 9.27: the port exists, so its type is NetBox's business, not the sheet's.

        Reported rather than silently dropped, because a sheet that states a type is making a claim
        and the operator should see that the claim was not acted on. Never an error: 9.8 forbids
        re-typing an interface this importer did not create, so there is nothing to reconcile.
        """
        if end.kind == PANEL:
            return
        stated = row.get(end.type_column).strip()
        if not stated:
            return
        held = choice_value(end.termination.get("type"))
        if not held or fold(held) == fold(stated):
            return
        collector.add(
            Severity.INFO,
            Code.INTERFACE_TYPE_IGNORED,
            line=row.line,
            column=end.type_column,
            value=stated,
            expected=f"{held!r}, or a blank cell",
            message=(
                f"{end.description} already exists in NetBox as a {held!r} interface, so "
                f"{end.type_column} was not used. This importer never changes the type of an "
                f"interface it did not create; NetBox's value stands."
            ),
        )

    # --- creating a missing interface (D12) -------------------------------------------------

    def _plan_interface(
        self,
        end: End,
        row: Row,
        device: dict[str, Any],
        device_name: str,
        lookups: Lookups,
        collector: IssueCollector,
        *,
        target_name: str | None = None,
        target_column: str | None = None,
    ) -> bool:
        """Resolve a port that does not exist into one this run will create, or refuse the row.

        Returns True when the end now has a planned interface on it. The order of the two sources
        is the safety property of D12 and not a preference — see the module docstring.
        """
        name_in_sheet = target_name or end.port
        issue_column = target_column or end.port_column
        if not settings.NETBOX_AUTOCREATE_INTERFACES or end.kind == PANEL:
            self._report_port_not_found(
                end,
                row,
                device,
                device_name,
                lookups,
                collector,
                target_name=name_in_sheet,
                target_column=issue_column,
            )
            return False

        device_id = int(device["id"])
        already = self._planned.get((device_id, fold(name_in_sheet)))
        if already is not None:
            # Requirement 9.30: however many rows or groups name it, it is one interface, created
            # once and warned about once.
            end.planned = already
            return True

        template = self._templates.get(
            (related_id(device.get("device_type")) or 0, fold(name_in_sheet))
        )
        if template is not None:
            payload = self._payload_from_template(template)
            source, type_value = FROM_TEMPLATE, choice_value(template.get("type"))
            name = str(template.get("name") or name_in_sheet)
        else:
            stated = row.get(end.type_column).strip()
            if not stated:
                self._report_type_required(
                    end,
                    row,
                    device_name,
                    collector,
                    target_name=name_in_sheet,
                )
                return False
            payload = {"name": name_in_sheet, "type": fold(stated)}
            source, type_value = FROM_SHEET, fold(stated)
            name = name_in_sheet

        interface = PlannedInterface(
            device_id=device_id,
            device_name=device_name,
            name=name,
            type_value=type_value,
            source=source,
            payload={**payload, "device": device_id, "name": name},
        )
        self._planned[(device_id, fold(name_in_sheet))] = interface
        end.planned = interface

        # Requirement 9.28: a mistyped port name and a genuinely new one are identical from here,
        # so the operator is the only thing standing between a typo and an object in NetBox.
        collector.add(
            Severity.WARNING,
            Code.INTERFACE_WILL_BE_CREATED,
            line=row.line,
            column=issue_column,
            value=name_in_sheet,
            expected="an interface that already exists, or consent to creating this one",
            message=(
                f"Interface {name_in_sheet!r} on {device_name!r} does not exist and will be "
                f"created as a {type_value!r} "
                f"interface, with the type taken from {source}. Check the spelling before "
                f"applying: a mistyped port name looks exactly like a new one from here."
            ),
            related_object=f"Device {device_name}",
        )
        return True

    @staticmethod
    def _payload_from_template(template: dict[str, Any]) -> dict[str, Any]:
        """The interface the device type would have produced (Requirement 9.25).

        Only keys the template actually carries are copied, so a NetBox that names one of them
        differently contributes nothing rather than something wrong.
        """
        payload: dict[str, Any] = {}
        for name in TEMPLATE_CHOICE_FIELDS:
            value = choice_value(template.get(name))
            if value:
                payload[name] = value
        for name in TEMPLATE_PLAIN_FIELDS:
            value = template.get(name)
            if value not in (None, ""):
                payload[name] = value
        return payload

    def _report_type_required(
        self,
        end: End,
        row: Row,
        device_name: str,
        collector: IssueCollector,
        *,
        target_name: str | None = None,
    ) -> None:
        """Requirement 9.26: both remedies, because either one is a reasonable thing to do."""
        name = target_name or end.port
        collector.add(
            Severity.ERROR,
            Code.INTERFACE_TYPE_REQUIRED,
            line=row.line,
            column=end.type_column,
            value="(blank)",
            expected=f"an interface type for {name!r}",
            message=(
                f"The {end.label} port {name!r} is not an interface on {device_name!r}, and "
                f"that device's type does not define one by that name either — so there is "
                f"nothing to copy a type from. Either add {name!r} to the device type as an "
                f"interface template, which fixes every device of that type at once, or fill in "
                f"{end.type_column} on this row. The type is never defaulted."
            ),
            related_object=f"Device {device_name}",
        )

    def _report_port_not_found(
        self,
        end: End,
        row: Row,
        device: dict[str, Any],
        device_name: str,
        lookups: Lookups,
        collector: IssueCollector,
        *,
        target_name: str | None = None,
        target_column: str | None = None,
    ) -> None:
        # Requirement 9.5: say which side and which port name, because a panel row has four
        # port columns and "not found" alone does not point at one of them.
        if end.kind == PANEL:
            hint = f" If you meant the other face, set {end.side}_port_side."
        elif not settings.NETBOX_AUTOCREATE_INTERFACES:
            hint = (
                " Add it in NetBox first, or set NETBOX_AUTOCREATE_INTERFACES to let a sheet "
                "create an interface its device type defines."
            )
        else:
            hint = ""
        name = target_name or end.port
        column = target_column or end.port_column
        collector.add(
            Severity.ERROR,
            Code.PORT_NOT_FOUND,
            line=row.line,
            column=column,
            value=name,
            expected=f"the name of an existing {end.port_label} on {device_name!r}",
            message=(
                f"The {end.label} port {name!r} is not a {end.port_label} on {device_name!r}.{hint}"
            ),
            related_object=f"Device {device_name}",
            related_url=self._url(lookups, DEVICES, device),
        )

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

        if settings.NETBOX_AUTOCREATE_INTERFACES and end.kind != PANEL:
            # Requirement 9.32: an interface that appears between validation and apply must abort
            # the run rather than be posted as a duplicate name. One key per port either way, so
            # the two validations compare like with like and the difference reads as a change of
            # value — `absent → id=...` — rather than as a key nobody can interpret.
            state[f"{INTERFACES}:{fold(end.name)}/{fold(end.cable_port)}"] = (
                "absent" if end.planned is not None else f"id={end.object_id}"
            )
            if end.has_parent:
                child_id = int(end.child["id"]) if end.child is not None else None
                state[f"{INTERFACES}:{fold(end.name)}/{fold(end.port)}"] = (
                    "absent" if end.planned_child is not None else f"id={child_id}"
                )

        if end.planned is not None:
            return

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
                if end.planned is not None:
                    # An interface that does not exist yet cannot be carrying a cable. Saying so
                    # here rather than asking the cache keeps the tri-state of 9.22 honest for a
                    # breakout whose lanes are part existing and part new.
                    found.free.append(end)
                    continue
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
                column=end.parent_column if end.has_parent else end.port_column,
                value=end.parent_value if end.has_parent else end.port,
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
            # Keyed on the termination key rather than on the id, because a planned interface has
            # no id yet and two of them would otherwise both key as None and be deduped into one.
            key=Registry.key(
                *(part for end in sides["a"] + sides["b"] for part in _termination_key(end))
            ),
            label=_cable_label(sides),
            line=lines[0],
            lines=lines,
            object_kind="cable",
            phase=CABLE_PHASE,
        )

    @staticmethod
    def _url(lookups: Lookups, endpoint: str, existing: dict[str, Any]) -> str:
        object_id = existing.get("id")
        return lookups.client.object_url(endpoint, int(object_id)) if object_id else ""
