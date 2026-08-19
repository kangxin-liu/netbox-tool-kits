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

A cable's identity is its pair of terminations, which is why an occupied port is a skip rather than
a create (Requirements 9.9, 11.1): the port already carries a cable, and iteration 1 never modifies
or deletes one.
"""

import re
from dataclasses import dataclass
from typing import Any, ClassVar

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
    )


def describe_cable(cable: dict[str, Any]) -> str:
    """Name an existing cable well enough for an operator to find it (Requirement 16.7)."""
    label = str(cable.get("label") or cable.get("display") or "").strip()
    return f"Cable #{cable.get('id')}" + (f" ({label})" if label else "")


class CablingImporter(BaseImporter):
    name = "cabling"
    endpoint_order = (CABLES,)

    choice_sources: ClassVar[dict[str, tuple[str, str]]] = {
        "cable_type": (CABLES, "type"),
        "cable_status": (CABLES, "status"),
        "length_unit": (CABLES, "length_unit"),
    }
    """`*_end_kind` and `*_port_side` are absent on purpose: they are this tool's own vocabulary
    for reading the sheet, not NetBox fields, so they validate against `columns.py` (Req 9.1)."""

    # --- S2 ---------------------------------------------------------------------------------

    def extra_cell_checks(self, row: Row) -> list[RowError]:
        errors: list[RowError] = []
        specs = self.specs()

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

    # --- S3 ---------------------------------------------------------------------------------

    def check_consistency(self, rows: list[Row], collector: IssueCollector) -> None:
        seen: dict[tuple[str, str, str], tuple[int, str]] = {}

        for row in rows:
            ends = self._ends(row)
            a_end, b_end = ends

            # Requirement 9.10: a cable between a port and itself is not a cable.
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
                    seen.setdefault(end.signature, (row.line, end.description))
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
                        f"this row was rejected."
                    ),
                    related_line=earlier[0],
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
        planned = PlannedRow(line=row.line)
        ends = [
            self._resolve(end, row, lookups, registry, state, collector, planned)
            for end in self._ends(row)
        ]
        if planned.outcome == RowOutcome.BLOCKED:
            return planned

        if self._already_cabled(ends, lookups, row, collector, planned):
            planned.outcome = RowOutcome.SKIP_EXISTS
            return planned

        planned.actions.append(self._cable(row, ends))
        planned.outcome = RowOutcome.CREATE
        return planned

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

    def _already_cabled(
        self,
        ends: list[End],
        lookups: Lookups,
        row: Row,
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> bool:
        """Requirement 9.9: an occupied termination skips the row and names the cable holding it."""
        found = False
        for end in ends:
            cable = lookups.occupied(end.object_type, end.object_id)
            if cable is None:
                continue
            found = True
            name = describe_cable(cable)
            collector.add(
                Severity.INFO,
                Code.PORT_ALREADY_CABLED,
                line=row.line,
                column=end.port_column,
                value=end.port,
                expected="a termination with no cable on it",
                message=(
                    f"The {end.label}, {end.description}, already carries {name}. The row was "
                    f"skipped: this iteration never modifies or removes an existing cable."
                ),
                related_object=name,
                related_url=lookups.client.object_url(CABLES, int(cable["id"]))
                if cable.get("id")
                else "",
            )
            planned.resolved["object"] = name
        return found

    def _cable(self, row: Row, ends: list[End]) -> CreateAction:
        a_end, b_end = ends
        payload: dict[str, Any] = {
            # Requirement 9.6: terminations are arrays on the cable itself. The
            # cable-terminations endpoint is read-only and is never written to.
            "a_terminations": [{"object_type": a_end.object_type, "object_id": a_end.object_id}],
            "b_terminations": [{"object_type": b_end.object_type, "object_id": b_end.object_id}],
            "type": fold(row.get("cable_type")),
            # Requirement 9.12.
            "status": fold(row.get("cable_status")) or DEFAULT_STATUS,
        }

        if row.get("label").strip():
            payload["label"] = row.get("label").strip()
        length = row.get("length").strip()
        if length:
            payload["length"] = int(length)
            payload["length_unit"] = fold(row.get("length_unit"))
        if row.get("color").strip():
            payload["color"] = fold(row.get("color"))

        label = f"{a_end.name}:{a_end.port} ↔ {b_end.name}:{b_end.port}"
        return CreateAction(
            endpoint=CABLES,
            payload=payload,
            key=Registry.key(
                a_end.object_type,
                a_end.object_id,
                b_end.object_type,
                b_end.object_id,
            ),
            label=label,
            line=row.line,
            object_kind="cable",
        )

    @staticmethod
    def _url(lookups: Lookups, endpoint: str, existing: dict[str, Any]) -> str:
        object_id = existing.get("id")
        return lookups.client.object_url(endpoint, int(object_id)) if object_id else ""
