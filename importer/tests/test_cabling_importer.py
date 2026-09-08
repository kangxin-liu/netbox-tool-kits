# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 15 — the cabling importer.

Requirements 9.1–9.12, 10.1, 10.6, 10.7, 10.14, 10.18, 11.3, 11.7, 12.1, 15.2, 16.6.

Every name here is synthetic: `exa1-*` devices, `swp*` interfaces, `MPO*` panel ports.
"""

import csv
import io
from dataclasses import dataclass, field
from typing import Any

import pytest

from importer.columns import columns_for
from importer.importers.base import ApplyRefused, RowOutcome
from importer.importers.cabling import CABLES, DEVICES, CablingImporter, read_end
from importer.services.lookups import Lookups
from importer.services.sheets import Row, parse_upload
from importer.tests import fakes
from importer.tests.fakes import FakeNetBox
from importer.validation import Code, Severity

INTERFACES = "dcim.interfaces"
FRONT_PORTS = "dcim.front-ports"
REAR_PORTS = "dcim.rear-ports"
CABLE_TERMINATIONS = "dcim.cable-terminations"

LEAF = "exa1-dh1-r01-leaf01"
PANEL = "exa1-dh1-r01-pp01"

BASE_ROW = {
    "a_end": LEAF,
    "a_end_kind": "device",
    "a_port": "swp1",
    "b_end": PANEL,
    "b_end_kind": "panel",
    "b_port": "MPO1",
    "cable_type": "smf-os2",
}


def sheet(
    rows: list[dict[str, str]],
    filename: str = "cabling.csv",
    extra_columns: dict[str, str] | None = None,
):
    """Build a real csv and parse it, so tests exercise the same path an upload takes."""
    specs = columns_for("cabling")
    extra = dict(extra_columns or {})
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([spec.label for spec in specs] + list(extra))
    for row_values in rows:
        writer.writerow(
            [row_values.get(spec.key, "") for spec in specs] + [extra[name] for name in extra]
        )
    payload = buffer.getvalue().encode("utf-8-sig")
    return parse_upload(
        "cabling",
        filename=filename,
        stream=io.BytesIO(payload),
        size=len(payload),
        content_type="text/csv",
    )


def row(**overrides: str) -> dict[str, str]:
    return {**BASE_ROW, **overrides}


def add_device(netbox: FakeNetBox, name: str, **fields: Any) -> dict[str, Any]:
    return netbox.add(DEVICES, name=name, **fields)


def add_port(
    netbox: FakeNetBox,
    endpoint: str,
    device: dict[str, Any],
    name: str,
    cable: Any = None,
    **fields: Any,
) -> dict[str, Any]:
    return netbox.add(
        endpoint,
        name=name,
        device={"id": device["id"], "name": device["name"]},
        cable=cable,
        **fields,
    )


@dataclass
class Topology:
    """A leaf switch and a patch panel, both of which the importer expects to find already there."""

    leaf: dict[str, Any]
    panel: dict[str, Any]
    interfaces: dict[str, dict[str, Any]] = field(default_factory=dict)
    front_ports: dict[str, dict[str, Any]] = field(default_factory=dict)
    rear_ports: dict[str, dict[str, Any]] = field(default_factory=dict)


@pytest.fixture
def netbox():
    return FakeNetBox()


@pytest.fixture
def importer():
    return CablingImporter()


@pytest.fixture
def topology(netbox) -> Topology:
    """Four ports a side, and the same names on both faces of the panel, as real panels have."""
    leaf = add_device(netbox, LEAF)
    panel = add_device(netbox, PANEL)
    built = Topology(leaf=leaf, panel=panel)
    for index in range(1, 5):
        # A device type gives an inherited interface its type; Requirement 9.8 forbids touching it.
        built.interfaces[f"swp{index}"] = add_port(
            netbox, INTERFACES, leaf, f"swp{index}", type={"value": "100gbase-x-qsfp28"}
        )
        built.front_ports[f"MPO{index}"] = add_port(netbox, FRONT_PORTS, panel, f"MPO{index}")
        built.rear_ports[f"MPO{index}"] = add_port(netbox, REAR_PORTS, panel, f"MPO{index}")
    return built


def validate(importer, netbox, rows):
    return importer.validate(sheet(rows), Lookups(client=netbox))


def apply_plan(importer, netbox, plan):
    return importer.apply(plan, Lookups(client=netbox))


def cable_payload(plan):
    return plan.rows[0].actions[0].payload


def record_fetches(netbox: FakeNetBox) -> list[str]:
    """Record the endpoints fetched, which is the budget Requirement 15.2 is about.

    Counting fetches rather than total requests keeps the assertion about the lookups the importer
    chooses to make, independently of the one OPTIONS request per endpoint that choices cost.
    """
    fetched: list[str] = []
    real = netbox.get_all

    def spy(endpoint: str, **filters: Any) -> list[dict[str, Any]]:
        fetched.append(endpoint)
        return real(endpoint, **filters)

    netbox.get_all = spy
    return fetched


# --- S2: cells ------------------------------------------------------------------------------


class TestCellChecks:
    def test_a_missing_required_value_blocks(self, importer, netbox, topology):
        plan = validate(importer, netbox, [row(a_port="")])
        error = next(i for i in plan.verdict.errors if i.code is Code.REQUIRED_VALUE_BLANK)
        assert error.column == "a_port"

    def test_a_cable_type_netbox_does_not_accept_is_rejected_with_the_accepted_values(
        self, importer, netbox, topology
    ):
        """Requirement 9.7: the choices come from the API, not from a hardcoded list."""
        plan = validate(importer, netbox, [row(cable_type="wet-string")])
        error = next(i for i in plan.verdict.errors if i.code is Code.VALUE_NOT_IN_CHOICES)
        assert error.column == "cable_type"
        assert "wet-string" in error.message
        assert "smf-os2" in error.expected

    def test_a_cable_type_netbox_added_is_accepted_even_though_the_sheet_never_listed_it(
        self, importer, netbox, topology
    ):
        """Requirement 9.7: `power` is in NetBox's choices but not in `columns.py`."""
        plan = validate(importer, netbox, [row(cable_type="power")])
        assert plan.verdict.is_applyable() is True
        assert cable_payload(plan)["type"] == "power"

    def test_a_cable_type_is_accepted_case_insensitively(self, importer, netbox, topology):
        plan = validate(importer, netbox, [row(cable_type="SMF-OS2")])
        assert plan.verdict.is_applyable() is True
        assert cable_payload(plan)["type"] == "smf-os2"

    def test_an_end_kind_that_is_neither_device_nor_panel_is_rejected(
        self, importer, netbox, topology
    ):
        """Requirement 9.1: the two kinds are this tool's own vocabulary, not a NetBox field."""
        plan = validate(importer, netbox, [row(a_end_kind="patchpanel")])
        error = next(i for i in plan.verdict.errors if i.code is Code.VALUE_NOT_IN_CHOICES)
        assert error.column == "a_end_kind"
        assert "device" in error.expected and "panel" in error.expected

    def test_a_port_side_that_is_neither_front_nor_rear_is_rejected(
        self, importer, netbox, topology
    ):
        plan = validate(importer, netbox, [row(b_port_side="sideways")])
        error = next(i for i in plan.verdict.errors if i.code is Code.VALUE_NOT_IN_CHOICES)
        assert error.column == "b_port_side"

    def test_a_length_without_a_length_unit_is_rejected(self, importer, netbox, topology):
        """Requirement 9.12: NetBox cannot record a length without its unit."""
        plan = validate(importer, netbox, [row(length="3")])
        error = next(i for i in plan.verdict.errors if i.code is Code.LENGTH_UNIT_MISSING)
        assert error.column == "length_unit"
        assert "without a length_unit" in error.message

    def test_a_length_unit_netbox_does_not_accept_is_rejected(self, importer, netbox, topology):
        """Requirement 9.12."""
        plan = validate(importer, netbox, [row(length="3", length_unit="fathoms")])
        error = next(i for i in plan.verdict.errors if i.code is Code.VALUE_NOT_IN_CHOICES)
        assert error.column == "length_unit"

    def test_a_length_that_is_not_a_positive_number_is_rejected(self, importer, netbox, topology):
        plan = validate(importer, netbox, [row(length="0", length_unit="m")])
        assert {i.code for i in plan.verdict.errors} == {Code.NOT_A_POSITIVE_INTEGER}

    @pytest.mark.parametrize("colour", ["#2196f3", "2196f", "2196f33", "ghijkl", "blue"])
    def test_a_colour_that_is_not_six_bare_hex_digits_is_rejected(
        self, importer, netbox, topology, colour
    ):
        plan = validate(importer, netbox, [row(color=colour)])
        error = next(i for i in plan.verdict.errors if i.column == "color")
        assert "six hex digits" in error.message
        assert "#" in error.expected

    def test_every_problem_in_the_stage_is_reported_not_just_the_first(
        self, importer, netbox, topology
    ):
        """Requirement 10.3."""
        plan = validate(
            importer,
            netbox,
            [row(cable_type="wet-string"), row(a_port="swp2", color="nope")],
        )
        assert {issue.line for issue in plan.verdict.errors} == {2, 3}

    def test_later_stages_are_not_reached_when_cells_are_wrong(self, importer, netbox, topology):
        """Requirement 10.4."""
        plan = validate(importer, netbox, [row(cable_type="wet-string")])
        assert plan.verdict.stages_completed == ("S1", "S2")
        assert plan.verdict.stages_not_reached == ("S3", "S4", "S5")


# --- 9.1–9.3: how each end is read ----------------------------------------------------------


class TestEndKindAndSide:
    def test_a_device_end_resolves_its_port_against_the_devices_interfaces(
        self, importer, netbox, topology
    ):
        """Requirement 9.2."""
        plan = validate(importer, netbox, [row()])
        assert cable_payload(plan)["a_terminations"] == [
            {"object_type": "dcim.interface", "object_id": topology.interfaces["swp1"]["id"]}
        ]

    def test_a_panel_end_resolves_against_front_ports_when_the_side_is_blank(
        self, importer, netbox, topology
    ):
        """Requirement 9.3: blank means front."""
        plan = validate(importer, netbox, [row(b_port_side="")])
        assert cable_payload(plan)["b_terminations"] == [
            {"object_type": "dcim.frontport", "object_id": topology.front_ports["MPO1"]["id"]}
        ]

    def test_a_panel_end_resolves_against_front_ports_when_the_side_is_front(
        self, importer, netbox, topology
    ):
        """Requirement 9.3."""
        plan = validate(importer, netbox, [row(b_port_side="front")])
        assert cable_payload(plan)["b_terminations"][0]["object_type"] == "dcim.frontport"

    def test_a_panel_end_resolves_against_rear_ports_when_the_side_is_rear(
        self, importer, netbox, topology
    ):
        """Requirement 9.3."""
        plan = validate(importer, netbox, [row(b_port_side="rear")])
        assert cable_payload(plan)["b_terminations"] == [
            {"object_type": "dcim.rearport", "object_id": topology.rear_ports["MPO1"]["id"]}
        ]

    def test_the_two_faces_of_a_panel_are_kept_apart(self, importer, netbox, topology):
        """Requirement 9.3: one name on both faces is normal, and they are not one port."""
        assert topology.front_ports["MPO1"]["id"] != topology.rear_ports["MPO1"]["id"]
        front = validate(importer, netbox, [row(b_port_side="front")])
        rear = validate(importer, netbox, [row(b_port_side="rear")])
        assert (
            cable_payload(front)["b_terminations"][0]["object_id"]
            != cable_payload(rear)["b_terminations"][0]["object_id"]
        )

    def test_a_port_side_on_a_device_end_is_ignored(self, importer, netbox, topology):
        """Requirement 9.3: the column describes a panel's faces; a device has interfaces only."""
        plan = validate(importer, netbox, [row(a_port_side="rear")])
        assert cable_payload(plan)["a_terminations"] == [
            {"object_type": "dcim.interface", "object_id": topology.interfaces["swp1"]["id"]}
        ]

    def test_the_documented_default_for_a_blank_end_kind_is_device(self):
        """Requirement 9.1: blank means `device`, which selects the interface port model."""
        end = read_end(Row(line=2, values=row(a_end_kind="")), "a")
        assert end.kind == "device"
        assert end.object_type == "dcim.interface"

    def test_a_blank_end_kind_validates_as_a_device_end(self, importer, netbox, topology):
        """Requirement 9.1, end to end.

        The column is optional precisely so this path is reachable: it was declared required in an
        earlier draft, and S2 rejects a required column's blank cell before a default can apply.
        """
        plan = validate(importer, netbox, [row(a_end_kind="")])
        assert plan.verdict.is_applyable() is True
        assert cable_payload(plan)["a_terminations"] == [
            {"object_type": "dcim.interface", "object_id": topology.interfaces["swp1"]["id"]}
        ]

    def test_the_end_kind_column_may_be_absent_from_the_sheet_entirely(
        self, importer, netbox, topology
    ):
        """An optional column can be left out of the file, not merely left blank (Req 5.4)."""
        values = row()
        del values["a_end_kind"]
        del values["b_end_kind"]
        values["b_end"] = topology.leaf["name"]
        values["b_port"] = "swp2"
        plan = validate(importer, netbox, [values])
        assert plan.verdict.is_applyable() is True
        assert cable_payload(plan)["b_terminations"][0]["object_type"] == "dcim.interface"

    def test_an_end_kind_is_read_case_insensitively(self, importer, netbox, topology):
        plan = validate(importer, netbox, [row(a_end_kind="Device", b_end_kind="PANEL")])
        assert plan.verdict.is_applyable() is True


# --- 9.4, 9.5: ends and ports that do not resolve --------------------------------------------


class TestUnresolvableEnds:
    def test_an_end_that_matches_no_device_rejects_the_row_naming_the_end(
        self, importer, netbox, topology
    ):
        """Requirement 9.4."""
        plan = validate(importer, netbox, [row(a_end="exa1-dh1-r01-leaf99")])
        error = next(i for i in plan.verdict.errors if i.code is Code.END_NOT_FOUND)
        assert error.column == "a_end"
        assert "A end" in error.message
        assert "exa1-dh1-r01-leaf99" in error.message
        assert plan.rows[0].outcome == RowOutcome.BLOCKED

    def test_an_end_that_matches_two_devices_rejects_the_row_as_ambiguous(
        self, importer, netbox, topology
    ):
        """Requirement 9.4: exactly one, or the cable cannot be placed."""
        add_device(netbox, LEAF)
        plan = validate(importer, netbox, [row()])
        error = next(i for i in plan.verdict.errors if i.code is Code.END_NOT_FOUND)
        assert "ambiguous" in error.message
        assert "2 devices" in error.message

    def test_both_ends_are_reported_when_neither_resolves(self, importer, netbox, topology):
        """Requirement 10.3: one round of fixes should be able to address both."""
        plan = validate(
            importer,
            netbox,
            [row(a_end="exa1-dh1-r01-leaf99", b_end="exa1-dh1-r01-pp99")],
        )
        columns = {i.column for i in plan.verdict.errors if i.code is Code.END_NOT_FOUND}
        assert columns == {"a_end", "b_end"}

    def test_an_end_is_matched_case_insensitively(self, importer, netbox, topology):
        """Requirement 11.1."""
        plan = validate(importer, netbox, [row(a_end=LEAF.upper())])
        assert plan.verdict.is_applyable() is True

    def test_a_port_that_does_not_exist_states_the_side_and_the_port_name(
        self, importer, netbox, topology
    ):
        """Requirement 9.5."""
        plan = validate(importer, netbox, [row(a_port="swp99")])
        error = next(i for i in plan.verdict.errors if i.code is Code.PORT_NOT_FOUND)
        assert error.column == "a_port"
        assert "A end" in error.message
        assert "swp99" in error.message
        assert LEAF in error.message

    def test_a_port_name_that_exists_only_on_the_other_face_is_not_found(
        self, importer, netbox, topology
    ):
        """Requirement 9.5: the side chooses the port model, so a rear-only name is missing."""
        add_port(netbox, REAR_PORTS, topology.panel, "MPO9")
        plan = validate(importer, netbox, [row(b_port="MPO9", b_port_side="front")])
        error = next(i for i in plan.verdict.errors if i.code is Code.PORT_NOT_FOUND)
        assert "front port" in error.message
        assert "b_port_side" in error.message

    def test_a_port_name_is_matched_case_insensitively(self, importer, netbox, topology):
        plan = validate(importer, netbox, [row(b_port="mpo1")])
        assert plan.verdict.is_applyable() is True

    def test_a_port_on_the_wrong_device_is_not_found(self, importer, netbox, topology):
        """Requirement 9.5: the port is resolved on the device the row named, not globally."""
        plan = validate(importer, netbox, [row(a_end=PANEL, a_end_kind="device")])
        error = next(i for i in plan.verdict.errors if i.code is Code.PORT_NOT_FOUND)
        assert "interface" in error.message
        assert PANEL in error.message


# --- S3: consistency within the file ---------------------------------------------------------


class TestConsistency:
    def test_a_row_terminating_both_ends_on_the_same_port_is_rejected(
        self, importer, netbox, topology
    ):
        """Requirement 9.10."""
        plan = validate(
            importer,
            netbox,
            [row(b_end=LEAF, b_end_kind="device", b_port="swp1")],
        )
        error = next(i for i in plan.verdict.errors if i.code is Code.SAME_PORT_BOTH_ENDS)
        assert error.line == 2
        assert "two different ports" in error.message

    def test_the_same_port_name_on_the_two_faces_of_one_panel_is_not_the_same_port(
        self, importer, netbox, topology
    ):
        """Requirement 9.10: a front-to-rear loop on one panel is odd but it is two ports."""
        plan = validate(
            importer,
            netbox,
            [
                row(
                    a_end=PANEL,
                    a_end_kind="panel",
                    a_port="MPO1",
                    a_port_side="front",
                    b_port_side="rear",
                )
            ],
        )
        assert not any(i.code is Code.SAME_PORT_BOTH_ENDS for i in plan.verdict.errors)

    def test_a_second_row_using_the_same_termination_names_the_earlier_line(
        self, importer, netbox, topology
    ):
        """Requirements 9.11, 16.6."""
        plan = validate(importer, netbox, [row(), row(b_port="MPO2")])
        error = next(i for i in plan.verdict.errors if i.code is Code.DUPLICATE_TERMINATION_IN_FILE)
        assert error.line == 3
        assert error.related_line == 2
        assert "line 2" in error.message
        assert error.column == "a_port"

    def test_a_reused_termination_is_found_across_the_a_and_b_columns(
        self, importer, netbox, topology
    ):
        """Requirement 9.11: the same port is the same port whichever end of a row names it."""
        plan = validate(
            importer,
            netbox,
            [row(), row(a_end=PANEL, a_end_kind="panel", a_port="MPO1", b_port="MPO2")],
        )
        error = next(i for i in plan.verdict.errors if i.code is Code.DUPLICATE_TERMINATION_IN_FILE)
        assert error.related_line == 2

    def test_a_reused_termination_is_found_regardless_of_casing(self, importer, netbox, topology):
        plan = validate(
            importer, netbox, [row(), row(a_end=LEAF.upper(), a_port="SWP1", b_port="MPO2")]
        )
        assert any(i.code is Code.DUPLICATE_TERMINATION_IN_FILE for i in plan.verdict.errors)

    def test_a_same_port_row_is_not_also_reported_as_its_own_duplicate(
        self, importer, netbox, topology
    ):
        """One mistake reads as one problem (Requirement 16.8)."""
        plan = validate(importer, netbox, [row(b_end=LEAF, b_end_kind="device", b_port="swp1")])
        assert not any(i.code is Code.DUPLICATE_TERMINATION_IN_FILE for i in plan.verdict.errors)

    def test_distinct_terminations_across_many_rows_are_left_alone(
        self, importer, netbox, topology
    ):
        rows = [row(a_port=f"swp{index}", b_port=f"MPO{index}") for index in range(1, 5)]
        plan = validate(importer, netbox, rows)
        assert plan.verdict.is_applyable() is True
        assert [planned.outcome for planned in plan.rows] == [RowOutcome.CREATE] * 4


# --- 9.6: the payload -------------------------------------------------------------------------


class TestCablePayload:
    def test_the_payload_carries_both_termination_arrays_and_the_cable_fields(
        self, importer, netbox, topology
    ):
        """Requirement 9.6."""
        plan = validate(importer, netbox, [row(b_port_side="rear")])
        payload = cable_payload(plan)
        assert payload == {
            "a_terminations": [
                {"object_type": "dcim.interface", "object_id": topology.interfaces["swp1"]["id"]}
            ],
            "b_terminations": [
                {"object_type": "dcim.rearport", "object_id": topology.rear_ports["MPO1"]["id"]}
            ],
            "type": "smf-os2",
            "status": "connected",
        }

    def test_cable_status_defaults_to_connected(self, importer, netbox, topology):
        """Requirement 9.12."""
        plan = validate(importer, netbox, [row(cable_status="")])
        assert cable_payload(plan)["status"] == "connected"

    def test_a_supplied_cable_status_overrides_the_default(self, importer, netbox, topology):
        plan = validate(importer, netbox, [row(cable_status="planned")])
        assert cable_payload(plan)["status"] == "planned"

    def test_the_optional_descriptive_fields_are_carried_through(self, importer, netbox, topology):
        plan = validate(
            importer,
            netbox,
            [row(label="LNK-0009", length="3", length_unit="m", color="2196F3")],
        )
        payload = cable_payload(plan)
        assert payload["label"] == "LNK-0009"
        assert payload["length"] == 3
        assert payload["length_unit"] == "m"
        assert payload["color"] == "2196f3"

    def test_blank_optional_fields_are_omitted_rather_than_sent_empty(
        self, importer, netbox, topology
    ):
        payload = cable_payload(validate(importer, netbox, [row()]))
        assert set(payload) == {"a_terminations", "b_terminations", "type", "status"}

    def test_nothing_is_written_to_the_read_only_cable_terminations_endpoint(
        self, importer, netbox, topology
    ):
        """Requirement 9.6."""
        plan = validate(importer, netbox, [row(), row(a_port="swp2", b_port="MPO2")])
        apply_plan(importer, netbox, plan)
        assert netbox.posted(CABLE_TERMINATIONS) == []
        assert {post.endpoint for post in netbox.posts} == {CABLES}
        assert netbox.patches == []


# --- 9.8 and decision D3: no transceivers, no interface changes -------------------------------


class TestNoTransceiverHandling:
    def test_the_sheet_has_no_transceiver_column_at_all(self):
        """Requirement 9.8, decision D3: deferred to iteration 2, not merely unused."""
        assert "transceiver" not in {spec.key for spec in columns_for("cabling")}

    def test_a_transceiver_column_in_an_upload_is_reported_as_ignored_and_changes_nothing(
        self, importer, netbox, topology
    ):
        """Requirements 9.8, 5.6: an unrecognised column is informational, and never blocks."""
        parsed = sheet([row()], extra_columns={"transceiver": "400G-DR4"})
        plan = importer.validate(parsed, Lookups(client=netbox))

        ignored = next(i for i in plan.verdict.infos if i.code is Code.UNKNOWN_COLUMN_IGNORED)
        assert ignored.value == "transceiver"
        assert plan.verdict.is_applyable() is True
        assert "400G-DR4" not in str(cable_payload(plan))

    def test_applying_a_sheet_leaves_every_interface_exactly_as_it_was(
        self, importer, netbox, topology
    ):
        """Requirements 9.8, 11.3: the interface's type is the device type's business, not ours.

        `cable` is excluded from the comparison because NetBox sets it itself when the cable is
        created — occupancy is its bookkeeping, not a write this tool issues. That the tool issues
        no write to the endpoint at all is the assertion below.
        """

        def without_occupancy(records):
            return [{k: v for k, v in record.items() if k != "cable"} for record in records]

        before = without_occupancy(netbox.stored(INTERFACES))
        plan = validate(importer, netbox, [row(), row(a_port="swp2", b_port="MPO2")])
        apply_plan(importer, netbox, plan)
        assert without_occupancy(netbox.stored(INTERFACES)) == before
        assert netbox.posted(INTERFACES) == []


# --- 9.9: a termination that already carries a cable ------------------------------------------


class TestAlreadyCabled:
    def test_a_port_that_is_already_cabled_is_skipped_rather_than_recabled(
        self, importer, netbox, topology
    ):
        """Requirement 9.9."""
        topology.interfaces["swp1"]["cable"] = {"id": 9001, "label": "LNK-0001"}
        plan = validate(importer, netbox, [row()])

        assert plan.verdict.is_applyable() is True
        assert plan.rows[0].outcome == RowOutcome.SKIP_EXISTS
        assert plan.rows[0].actions == []

    def test_the_skip_identifies_the_existing_cable_and_links_to_it(
        self, importer, netbox, topology
    ):
        """Requirements 9.9, 16.7."""
        topology.interfaces["swp1"]["cable"] = {"id": 9001, "label": "LNK-0001"}
        plan = validate(importer, netbox, [row()])

        info = next(i for i in plan.verdict.infos if i.code is Code.PORT_ALREADY_CABLED)
        assert info.severity is Severity.INFO
        assert "Cable #9001" in info.message
        assert "LNK-0001" in info.related_object
        assert info.related_url.endswith("/dcim/cables/9001/")
        assert "never modifies or removes" in info.message

    def test_an_occupied_b_end_skips_the_row_just_as_an_occupied_a_end_does(
        self, importer, netbox, topology
    ):
        """Requirement 9.9: *either* termination."""
        topology.front_ports["MPO1"]["cable"] = {"id": 9002}
        plan = validate(importer, netbox, [row()])
        assert plan.rows[0].outcome == RowOutcome.SKIP_EXISTS

    def test_a_cable_recorded_only_by_id_still_counts_as_occupied(self, importer, netbox, topology):
        topology.interfaces["swp1"]["cable"] = 9003
        plan = validate(importer, netbox, [row()])
        assert plan.rows[0].outcome == RowOutcome.SKIP_EXISTS

    def test_the_existing_cable_is_neither_modified_nor_deleted(self, importer, netbox, topology):
        """Requirements 9.9, 11.3."""
        existing = netbox.add(CABLES, label="LNK-0001", type="smf-os2")
        topology.interfaces["swp1"]["cable"] = {"id": existing["id"], "label": "LNK-0001"}

        plan = validate(importer, netbox, [row()])
        report = apply_plan(importer, netbox, plan)

        assert netbox.posts == []
        assert netbox.stored(CABLES) == [existing]
        assert report.created_count == 0
        assert report.skipped_count == 1

    def test_only_the_occupied_row_is_skipped_and_the_rest_still_create(
        self, importer, netbox, topology
    ):
        """Requirement 11.2: skipping what exists is not the same as blocking the file."""
        topology.interfaces["swp1"]["cable"] = {"id": 9004}
        plan = validate(importer, netbox, [row(), row(a_port="swp2", b_port="MPO2")])

        assert [planned.outcome for planned in plan.rows] == [
            RowOutcome.SKIP_EXISTS,
            RowOutcome.CREATE,
        ]
        report = apply_plan(importer, netbox, plan)
        assert report.created_count == 1
        assert report.skipped_count == 1

    def test_re_uploading_the_same_sheet_after_an_apply_creates_nothing(
        self, importer, netbox, topology
    ):
        """Requirement 11.7: the second run finds every row already cabled."""
        first = validate(importer, netbox, [row()])
        apply_plan(importer, netbox, first)
        created = netbox.stored(CABLES)[0]

        # NetBox marks both terminations as occupied once the cable exists.
        topology.interfaces["swp1"]["cable"] = {"id": created["id"]}
        topology.front_ports["MPO1"]["cable"] = {"id": created["id"]}

        second = validate(importer, netbox, [row()])
        assert second.verdict.errors == []
        assert second.rows[0].outcome == RowOutcome.SKIP_EXISTS

        report = apply_plan(importer, netbox, second)
        assert report.created_count == 0
        assert len(netbox.stored(CABLES)) == 1


# --- the validation gate ----------------------------------------------------------------------


class TestValidationGate:
    def test_nothing_is_written_during_validation(self, importer, netbox, topology):
        """Requirement 10.1."""
        validate(importer, netbox, [row(), row(a_port="swp2", b_port="MPO2")])
        assert netbox.posts == []
        assert netbox.stored(CABLES) == []

    def test_a_row_that_blocks_makes_no_row_in_the_file_applyable(self, importer, netbox, topology):
        """Requirement 10.7: not even the valid rows, and not even on request."""
        plan = validate(
            importer,
            netbox,
            [row(), row(a_port="swp2", b_end="exa1-dh1-r01-pp99", b_port="MPO2")],
        )
        assert plan.verdict.errors
        assert all(planned.outcome == RowOutcome.BLOCKED for planned in plan.rows)
        assert plan.verdict.create_count == 0
        assert "no partial apply" in plan.verdict.refusal_reason()

    def test_a_blocked_plan_cannot_be_applied_even_directly(self, importer, netbox, topology):
        """Requirement 10.6."""
        plan = validate(importer, netbox, [row(a_port="swp99")])
        with pytest.raises(ApplyRefused) as raised:
            apply_plan(importer, netbox, plan)
        assert "error" in raised.value.reason
        assert netbox.posts == []


# --- apply -------------------------------------------------------------------------------------


class TestApply:
    def test_the_whole_sheet_applies_as_one_bulk_post(self, importer, netbox, topology):
        """Decision D7: one endpoint, so one transactional batch and no half-applied window."""
        rows = [row(a_port=f"swp{index}", b_port=f"MPO{index}") for index in range(1, 5)]
        plan = validate(importer, netbox, rows)
        apply_plan(importer, netbox, plan)

        assert [post.endpoint for post in netbox.posts] == [CABLES]
        assert [post.size for post in netbox.posts] == [4]

    def test_the_created_cables_carry_the_termination_arrays_that_were_planned(
        self, importer, netbox, topology
    ):
        """Requirement 9.6."""
        plan = validate(importer, netbox, [row()])
        apply_plan(importer, netbox, plan)
        stored = netbox.stored(CABLES)[0]
        assert stored["a_terminations"][0]["object_id"] == topology.interfaces["swp1"]["id"]
        assert stored["b_terminations"][0]["object_type"] == "dcim.frontport"

    def test_the_report_lists_each_row_with_its_line_and_a_link(self, importer, netbox, topology):
        """Requirement 12.1."""
        plan = validate(importer, netbox, [row()])
        report = apply_plan(importer, netbox, plan)
        result = report.results[0]
        assert result.line == 2
        assert result.object_kind == "cable"
        assert f"{LEAF}:swp1" in result.object_name
        assert result.url.endswith(f"/dcim/cables/{result.object_id}/")

    def test_the_plan_records_what_each_end_and_port_resolved_to(self, importer, netbox, topology):
        """Requirement 10.18: the per-row table shows the resolved NetBox objects."""
        plan = validate(importer, netbox, [row(b_port_side="rear")])
        resolved = plan.rows[0].resolved
        assert resolved["a_end"] == f"{LEAF} (existing)"
        assert resolved["a_port"] == "swp1 (interface, existing)"
        assert resolved["b_end"] == f"{PANEL} (existing)"
        assert resolved["b_port"] == "MPO1 (rear port, existing)"

    def test_a_netbox_rejection_is_blamed_on_the_offending_row(self, importer, netbox, topology):
        """Requirement 12.2."""
        rows = [row(a_port=f"swp{index}", b_port=f"MPO{index}") for index in range(1, 4)]
        plan = validate(importer, netbox, rows)
        netbox.reject(
            CABLES,
            lambda payloads: (400, [{}, {"a_terminations": ["Already has a cable."]}, {}]),
        )
        report = apply_plan(importer, netbox, plan)

        blamed = next(r for r in report.results if r.column == "a_terminations")
        assert blamed.line == 3
        assert "Already has a cable" in blamed.message
        # Decision D7: the rejected batch rolled back, so nothing was created.
        assert netbox.stored(CABLES) == []


class TestDriftDetection:
    def test_a_port_cabled_between_validation_and_apply_aborts_before_writing(
        self, importer, netbox, topology
    ):
        """Requirement 10.14: a plan built on a free port is not safe once someone else took it."""
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable() is True

        topology.interfaces["swp1"]["cable"] = {"id": 9005}

        report = apply_plan(importer, netbox, plan)
        assert report.aborted is True
        assert "changed after this file was validated" in report.abort_reason
        assert "Nothing was written" in report.abort_reason
        assert netbox.posts == []

    def test_a_port_that_disappeared_between_validation_and_apply_aborts(
        self, importer, netbox, topology
    ):
        """Requirement 10.14."""
        plan = validate(importer, netbox, [row()])
        netbox.objects[FRONT_PORTS] = [
            record
            for record in netbox.stored(FRONT_PORTS)
            if record["id"] != topology.front_ports["MPO1"]["id"]
        ]
        report = apply_plan(importer, netbox, plan)
        assert report.aborted is True
        assert netbox.posts == []

    def test_an_unchanged_netbox_applies_normally(self, importer, netbox, topology):
        plan = validate(importer, netbox, [row()])
        report = apply_plan(importer, netbox, plan)
        assert report.aborted is False
        assert report.created_count == 1


# --- the request budget --------------------------------------------------------------------


class TestRequestBudget:
    def test_record_fetches_do_not_grow_with_the_row_count(self, importer, netbox):
        """Requirement 15.2: devices in one pass, then their ports in bulk."""
        leaf = add_device(netbox, LEAF)
        panel = add_device(netbox, PANEL)
        for index in range(1, 201):
            add_port(netbox, INTERFACES, leaf, f"swp{index}")
            add_port(netbox, FRONT_PORTS, panel, f"MPO{index}")

        fetched = record_fetches(netbox)
        importer.validate(sheet([row()]), Lookups(client=netbox))
        one_row = list(fetched)

        fetched.clear()
        rows = [row(a_port=f"swp{index}", b_port=f"MPO{index}") for index in range(1, 201)]
        plan = importer.validate(sheet(rows), Lookups(client=netbox))

        assert plan.verdict.is_applyable() is True
        assert len(fetched) == len(one_row)
        assert sorted(fetched) == sorted([DEVICES, INTERFACES, FRONT_PORTS])

    def test_a_sheet_naming_many_devices_resolves_all_of_their_ports_in_bulk(
        self, importer, netbox
    ):
        """Requirement 15.2: one request per port model, not one per device."""
        rows = []
        for index in range(1, 51):
            leaf = add_device(netbox, f"exa1-dh1-r{index:02d}-leaf01")
            panel = add_device(netbox, f"exa1-dh1-r{index:02d}-pp01")
            add_port(netbox, INTERFACES, leaf, "swp1")
            add_port(netbox, FRONT_PORTS, panel, "MPO1")
            rows.append(row(a_end=leaf["name"], b_end=panel["name"]))

        fetched = record_fetches(netbox)
        plan = importer.validate(sheet(rows), Lookups(client=netbox))

        assert plan.verdict.is_applyable() is True
        assert sorted(fetched) == sorted([DEVICES, INTERFACES, FRONT_PORTS])

    def test_rear_ports_are_not_fetched_when_no_row_names_a_rear_side(self, importer, netbox):
        """Requirement 15.2: the port models the sheet uses, and no others."""
        leaf = add_device(netbox, LEAF)
        panel = add_device(netbox, PANEL)
        add_port(netbox, INTERFACES, leaf, "swp1")
        add_port(netbox, FRONT_PORTS, panel, "MPO1")

        fetched = record_fetches(netbox)
        importer.validate(sheet([row()]), Lookups(client=netbox))
        assert REAR_PORTS not in fetched


# --- 9.13, 9.14: the cable_profile column (D10) ---------------------------------------------


class TestCableProfile:
    def test_a_blank_profile_leaves_the_key_out_of_the_payload_altogether(
        self, importer, netbox, topology
    ):
        """Requirement 9.13: a sheet that does not use profiles posts what it always posted."""
        plan = validate(importer, netbox, [row()])
        assert "profile" not in cable_payload(plan)

    def test_a_profile_reaches_the_payload(self, importer, netbox, topology):
        plan = validate(importer, netbox, [row(cable_profile="single-1c2p")])
        assert cable_payload(plan)["profile"] == "single-1c2p"

    def test_a_profile_netbox_does_not_accept_is_rejected_with_the_accepted_values(
        self, importer, netbox, topology
    ):
        """Requirement 9.13: the choices come from the API, as `cable_type`'s do."""
        plan = validate(importer, netbox, [row(cable_profile="breakout-1c99p-99c1p")])
        error = next(i for i in plan.verdict.errors if i.code is Code.VALUE_NOT_IN_CHOICES)
        assert error.column == "cable_profile"
        assert "breakout-1c4p-4c1p" in error.expected

    def test_a_profile_is_accepted_case_insensitively_and_normalised(
        self, importer, netbox, topology
    ):
        plan = validate(importer, netbox, [row(cable_profile="Single-1C2P")])
        assert cable_payload(plan)["profile"] == "single-1c2p"

    def test_a_netbox_reporting_no_profiles_at_all_blocks_a_named_profile(
        self, importer, netbox, topology, monkeypatch
    ):
        """Requirement 9.14: refuse the value rather than silently dropping it."""
        monkeypatch.setitem(fakes.CHOICES[CABLES], "profile", frozenset())
        plan = validate(importer, netbox, [row(cable_profile="single-1c2p")])
        error = next(i for i in plan.verdict.errors if i.code is Code.CABLE_PROFILE_UNSUPPORTED)
        assert error.column == "cable_profile"
        assert "no cable profile choices" in error.message
        assert netbox.posts == []

    def test_a_netbox_reporting_no_profiles_still_accepts_a_sheet_that_names_none(
        self, importer, netbox, topology, monkeypatch
    ):
        monkeypatch.setitem(fakes.CHOICES[CABLES], "profile", frozenset())
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable() is True


# --- 9.15–9.21: cable groups (D10) ----------------------------------------------------------

BREAKOUT = "breakout-1c4p-4c1p"


def breakout_rows(group: str = "brk-01", profile: str = BREAKOUT, count: int = 4):
    """One A-side port fanning out to `count` panel ports — the shape D10 exists for."""
    rows = []
    for index in range(1, count + 1):
        values = row(a_port="swp1", b_port=f"MPO{index}", cable_group=group)
        if index > 1:
            # Requirement 9.18: later rows inherit the cable's own values from the first.
            values["cable_profile"] = ""
        else:
            values["cable_profile"] = profile
        rows.append(values)
    return rows


class TestCableGroup:
    def test_four_rows_sharing_a_tag_become_one_cable(self, importer, netbox, topology):
        """Requirement 9.16: one A termination, four B terminations, one create."""
        plan = validate(importer, netbox, breakout_rows())

        actions = [action for planned in plan.rows for action in planned.actions]
        assert len(actions) == 1
        payload = actions[0].payload
        assert len(payload["a_terminations"]) == 1
        assert len(payload["b_terminations"]) == 4
        assert payload["profile"] == BREAKOUT

    def test_the_b_terminations_are_sent_in_row_order(self, importer, netbox, topology):
        """Requirement 9.21: row order is lane order, because it is the only lever there is."""
        plan = validate(importer, netbox, breakout_rows())
        payload = next(action for planned in plan.rows for action in planned.actions).payload
        assert [term["object_id"] for term in payload["b_terminations"]] == [
            topology.front_ports[f"MPO{index}"]["id"] for index in range(1, 5)
        ]

    def test_reordering_the_rows_reorders_the_lanes(self, importer, netbox, topology):
        rows = breakout_rows()
        reversed_rows = [rows[0], *reversed(rows[1:])]
        plan = validate(importer, netbox, reversed_rows)
        payload = next(action for planned in plan.rows for action in planned.actions).payload
        assert [term["object_id"] for term in payload["b_terminations"]] == [
            topology.front_ports[f"MPO{index}"]["id"] for index in (1, 4, 3, 2)
        ]

    def test_the_shared_a_port_is_not_a_duplicate_termination(self, importer, netbox, topology):
        """Requirement 9.17: inside a group the repetition is the connector they share."""
        plan = validate(importer, netbox, breakout_rows())
        assert plan.verdict.errors == []

    def test_without_the_group_tag_the_same_rows_are_three_duplicates(
        self, importer, netbox, topology
    ):
        """Requirement 9.11 still holds: nobody said those rows were one cable."""
        rows = [dict(values, cable_group="") for values in breakout_rows()]
        plan = validate(importer, netbox, rows)
        duplicates = [
            i for i in plan.verdict.errors if i.code is Code.DUPLICATE_TERMINATION_IN_FILE
        ]
        assert [issue.line for issue in duplicates] == [3, 4, 5]
        assert "same cable_group" in duplicates[0].message

    def test_the_same_port_reused_by_a_second_group_is_still_a_duplicate(
        self, importer, netbox, topology
    ):
        """Requirement 9.17 relaxes 9.11 *within* a group, not across two of them."""
        rows = [
            row(a_port="swp1", b_port="MPO1", cable_group="brk-01"),
            row(a_port="swp1", b_port="MPO2", cable_group="brk-02"),
        ]
        plan = validate(importer, netbox, rows)
        duplicate = next(
            i for i in plan.verdict.errors if i.code is Code.DUPLICATE_TERMINATION_IN_FILE
        )
        assert (duplicate.line, duplicate.related_line) == (3, 2)

    def test_a_group_tag_is_matched_case_insensitively(self, importer, netbox, topology):
        rows = [
            row(a_port="swp1", b_port="MPO1", cable_group="BRK-01", cable_profile=BREAKOUT),
            row(a_port="swp1", b_port="MPO2", cable_group="brk-01"),
        ]
        plan = validate(importer, netbox, rows)
        assert plan.verdict.errors == []
        assert plan.verdict.object_count == 1

    def test_both_ends_on_one_port_is_still_rejected_inside_a_group(
        self, importer, netbox, topology
    ):
        """Requirement 9.17: 9.10 is not relaxed."""
        rows = [
            row(a_port="swp1", b_port="MPO1", cable_group="brk-01", cable_profile=BREAKOUT),
            row(
                a_end=LEAF,
                a_end_kind="device",
                a_port="swp2",
                b_end=LEAF,
                b_end_kind="device",
                b_port="swp2",
                cable_group="brk-01",
            ),
        ]
        plan = validate(importer, netbox, rows)
        assert any(i.code is Code.SAME_PORT_BOTH_ENDS for i in plan.verdict.errors)

    def test_one_row_carrying_a_tag_is_an_ordinary_cable(self, importer, netbox, topology):
        plan = validate(importer, netbox, [row(cable_group="brk-01")])
        payload = cable_payload(plan)
        assert len(payload["a_terminations"]) == len(payload["b_terminations"]) == 1

    def test_the_verdict_counts_one_object_for_the_four_rows(self, importer, netbox, topology):
        """The apply button quotes objects, and four rows are one cable."""
        plan = validate(importer, netbox, breakout_rows())
        assert plan.verdict.create_count == 4
        assert plan.verdict.object_count == 1

    def test_rows_are_previewed_in_sheet_order_even_when_groups_interleave(
        self, importer, netbox, topology
    ):
        """Requirement 10.18: the preview reads down the file as the operator wrote it."""
        rows = [
            row(a_port="swp1", b_port="MPO1", cable_group="brk-01", cable_profile=BREAKOUT),
            row(a_port="swp2", b_port="MPO2", cable_group="brk-02", cable_profile=BREAKOUT),
            row(a_port="swp1", b_port="MPO3", cable_group="brk-01"),
            row(a_port="swp2", b_port="MPO4", cable_group="brk-02"),
        ]
        plan = validate(importer, netbox, rows)
        assert [planned.line for planned in plan.rows] == [2, 3, 4, 5]

    def test_the_preview_names_the_connector_each_row_will_get(self, importer, netbox, topology):
        """Requirement 9.21: lane assignment is read before apply, not discovered after."""
        plan = validate(importer, netbox, breakout_rows())
        assert plan.rows[0].resolved["connector"] == "B connector 1 of 4"
        assert plan.rows[3].resolved["connector"] == "B connector 4 of 4"

    def test_an_ungrouped_row_is_given_no_connector_to_read(self, importer, netbox, topology):
        plan = validate(importer, netbox, [row()])
        assert "connector" not in plan.rows[0].resolved

    def test_a_blocked_row_blocks_the_whole_cable(self, importer, netbox, topology):
        """One lane's port cannot be found, so the cable cannot be created as described."""
        rows = breakout_rows()
        rows[2]["b_port"] = "MPO99"
        plan = validate(importer, netbox, rows)
        assert all(planned.outcome == RowOutcome.BLOCKED for planned in plan.rows)
        assert netbox.posts == []


# --- 9.18: one cable, one set of values -----------------------------------------------------


class TestGroupValues:
    def test_later_rows_inherit_the_cables_values_from_the_first(self, importer, netbox, topology):
        rows = breakout_rows()
        rows[0]["label"] = "LNK-0001"
        rows[0]["cable_status"] = "planned"
        plan = validate(importer, netbox, rows)
        payload = next(action for planned in plan.rows for action in planned.actions).payload
        assert payload["label"] == "LNK-0001"
        assert payload["status"] == "planned"

    def test_a_value_supplied_only_by_a_later_row_is_still_the_cables_value(
        self, importer, netbox, topology
    ):
        rows = breakout_rows()
        rows[2]["label"] = "LNK-0002"
        plan = validate(importer, netbox, rows)
        payload = next(action for planned in plan.rows for action in planned.actions).payload
        assert payload["label"] == "LNK-0002"

    @pytest.mark.parametrize(
        ("column", "first", "second"),
        [
            ("cable_type", "smf-os2", "mmf-om4"),
            ("cable_profile", BREAKOUT, "breakout-1c8p-8c1p"),
            ("label", "LNK-0001", "LNK-0002"),
            ("cable_status", "connected", "planned"),
        ],
    )
    def test_two_rows_describing_the_cable_differently_are_rejected(
        self, importer, netbox, topology, column, first, second
    ):
        """Requirement 9.18: a cable has one type and one profile, and the tool picks neither."""
        rows = breakout_rows()
        rows[0][column] = first
        rows[1][column] = second
        plan = validate(importer, netbox, rows)

        conflict = next(i for i in plan.verdict.errors if i.code is Code.CABLE_GROUP_FIELD_CONFLICT)
        assert conflict.column == column
        assert (conflict.line, conflict.related_line) == (3, 2)
        assert first in conflict.message and second in conflict.message

    def test_a_required_column_may_be_left_blank_where_the_group_supplies_it(
        self, importer, netbox, topology
    ):
        """Requirement 9.18: required means required of the cable, not typed on every row.

        This is the case that forced normalisation to run before S2 — `cable_type` is required, so
        a blank cell would otherwise be rejected before anything could look for the group's value.
        """
        rows = breakout_rows()
        for values in rows[1:]:
            values["cable_type"] = ""
        plan = validate(importer, netbox, rows)

        assert plan.verdict.errors == []
        payload = next(action for planned in plan.rows for action in planned.actions).payload
        assert payload["type"] == "smf-os2"

    def test_an_ungrouped_row_still_has_to_state_its_required_values(
        self, importer, netbox, topology
    ):
        """Nothing inherits into a cable of one: there is no other row to inherit from."""
        plan = validate(importer, netbox, [row(cable_type="")])
        error = next(i for i in plan.verdict.errors if i.code is Code.REQUIRED_VALUE_BLANK)
        assert error.column == "cable_type"

    def test_a_blank_required_cell_in_a_group_that_never_states_it_is_still_rejected(
        self, importer, netbox, topology
    ):
        rows = [dict(values, cable_type="") for values in breakout_rows()]
        plan = validate(importer, netbox, rows)
        blanks = [i for i in plan.verdict.errors if i.code is Code.REQUIRED_VALUE_BLANK]
        assert [issue.line for issue in blanks] == [2, 3, 4, 5]

    def test_a_repeated_identical_value_is_not_a_conflict(self, importer, netbox, topology):
        rows = breakout_rows()
        for values in rows:
            values["cable_profile"] = BREAKOUT
        plan = validate(importer, netbox, rows)
        assert plan.verdict.errors == []

    def test_the_same_value_differently_cased_is_not_a_conflict(self, importer, netbox, topology):
        rows = breakout_rows()
        rows[1]["cable_type"] = "SMF-OS2"
        plan = validate(importer, netbox, rows)
        assert plan.verdict.errors == []

    def test_two_ungrouped_rows_may_of_course_differ(self, importer, netbox, topology):
        plan = validate(
            importer,
            netbox,
            [row(), row(a_port="swp2", b_port="MPO2", cable_type="mmf-om4")],
        )
        assert plan.verdict.errors == []


# --- 9.19, 9.20: the profile and the number of terminations ---------------------------------


class TestProfileFitsTheGroup:
    def test_a_side_with_more_terminations_than_connectors_is_rejected(
        self, importer, netbox, topology
    ):
        """Requirement 9.19: `1c2p-2c1p` has two B connectors, so a third lane will not fit."""
        rows = breakout_rows(profile="breakout-1c2p-2c1p", count=3)
        plan = validate(importer, netbox, rows)

        overflow = next(i for i in plan.verdict.errors if i.code is Code.PROFILE_CONNECTOR_OVERFLOW)
        assert overflow.column == "b_port"
        assert "at most 2" in overflow.expected
        assert "2 connectors of 1 position" in overflow.message

    def test_a_side_filled_exactly_to_its_connector_count_is_fine(self, importer, netbox, topology):
        plan = validate(importer, netbox, breakout_rows(count=4))
        assert plan.verdict.errors == []

    def test_a_partly_populated_breakout_is_allowed(self, importer, netbox, topology):
        """Two lanes of a four-lane breakout is a real cable, not an error (Requirement 9.19)."""
        plan = validate(importer, netbox, breakout_rows(count=2))
        assert plan.verdict.errors == []

    def test_the_a_side_is_checked_against_its_own_limit(self, importer, netbox, topology):
        """A breakout has one A connector, so two distinct A ports do not fit."""
        rows = [
            row(a_port="swp1", b_port="MPO1", cable_group="brk-01", cable_profile=BREAKOUT),
            row(a_port="swp2", b_port="MPO2", cable_group="brk-01"),
        ]
        plan = validate(importer, netbox, rows)
        overflow = next(i for i in plan.verdict.errors if i.code is Code.PROFILE_CONNECTOR_OVERFLOW)
        assert overflow.column == "a_port"
        assert "at most 1" in overflow.expected

    def test_a_trunk_permits_several_terminations_on_both_sides(self, importer, netbox, topology):
        rows = [
            row(a_port="swp1", b_port="MPO1", cable_group="trk-01", cable_profile="trunk-2c4p"),
            row(a_port="swp2", b_port="MPO2", cable_group="trk-01"),
        ]
        plan = validate(importer, netbox, rows)
        assert plan.verdict.errors == []
        payload = next(action for planned in plan.rows for action in planned.actions).payload
        assert len(payload["a_terminations"]) == len(payload["b_terminations"]) == 2

    def test_a_profile_whose_shape_cannot_be_read_warns_instead_of_guessing(
        self, importer, netbox, topology, monkeypatch
    ):
        """Requirement 9.19: NetBox accepts it, so do not invent a limit and block it."""
        monkeypatch.setitem(
            fakes.CHOICES[CABLES], "profile", frozenset({"mystery-profile", BREAKOUT})
        )
        rows = breakout_rows(profile="mystery-profile")
        plan = validate(importer, netbox, rows)

        assert plan.verdict.errors == []
        warning = next(i for i in plan.verdict.warnings if i.code is Code.PROFILE_STRUCTURE_UNKNOWN)
        assert "cannot read how many connectors" in warning.message

    def test_a_grouped_cable_with_no_profile_warns_that_lanes_cannot_be_traced(
        self, importer, netbox, topology
    ):
        """Requirement 9.20: NetBox will store it; it just cannot trace it."""
        rows = [dict(values, cable_profile="") for values in breakout_rows()]
        plan = validate(importer, netbox, rows)

        assert plan.verdict.errors == []
        warning = next(
            i for i in plan.verdict.warnings if i.code is Code.CABLE_GROUP_WITHOUT_PROFILE
        )
        assert "trace which lane" in warning.message
        assert plan.verdict.is_applyable() is False

        plan.verdict.acknowledge()
        assert plan.verdict.is_applyable() is True

    def test_an_ordinary_row_without_a_profile_is_not_warned_about(
        self, importer, netbox, topology
    ):
        """One termination a side needs no profile: there is only one lane to find."""
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.warnings == []


# --- 9.22, 9.23: occupancy and reporting over a whole group ---------------------------------


class TestGroupOccupancy:
    def cable_the_group(self, importer, netbox, topology, rows=None):
        plan = validate(importer, netbox, rows or breakout_rows())
        apply_plan(importer, netbox, plan)
        return netbox.stored(CABLES)[0]

    def test_the_group_applies_as_a_single_post(self, importer, netbox, topology):
        plan = validate(importer, netbox, breakout_rows())
        apply_plan(importer, netbox, plan)
        assert [post.size for post in netbox.posted(CABLES)] == [1]

    def test_every_line_of_the_group_gets_a_result_naming_the_one_cable(
        self, importer, netbox, topology
    ):
        """Requirement 9.23."""
        plan = validate(importer, netbox, breakout_rows())
        report = apply_plan(importer, netbox, plan)

        created = report.created
        assert [result.line for result in created] == [2, 3, 4, 5]
        assert len({result.object_id for result in created}) == 1
        assert "one object for lines 2, 3, 4 and 5" in created[1].message

    def test_the_report_counts_one_object_not_four(self, importer, netbox, topology):
        plan = validate(importer, netbox, breakout_rows())
        report = apply_plan(importer, netbox, plan)
        assert report.created_count == 1
        assert len(netbox.stored(CABLES)) == 1

    def test_only_one_line_carries_the_write_sequence(self, importer, netbox, topology):
        """Requirement 12.4: a manual unwind finds each object once."""
        plan = validate(importer, netbox, breakout_rows())
        report = apply_plan(importer, netbox, plan)
        assert [result.sequence for result in report.created] == [1, 0, 0, 0]

    def test_re_uploading_an_applied_group_creates_nothing(self, importer, netbox, topology):
        """Requirement 9.22: every termination is held by the same cable, so the group skips."""
        self.cable_the_group(importer, netbox, topology)

        second = validate(importer, netbox, breakout_rows())
        assert second.verdict.errors == []
        assert all(planned.outcome == RowOutcome.SKIP_EXISTS for planned in second.rows)

        report = apply_plan(importer, netbox, second)
        assert report.created_count == 0
        assert report.skipped_count == 4
        assert len(netbox.stored(CABLES)) == 1

    def test_a_partly_cabled_group_blocks_rather_than_skipping_the_missing_lanes(
        self, importer, netbox, topology
    ):
        """Requirement 9.22: the lanes that are missing would be lost silently."""
        existing = netbox.add(CABLES, label="LNK-0001")
        topology.front_ports["MPO1"]["cable"] = {"id": existing["id"], "label": "LNK-0001"}

        plan = validate(importer, netbox, breakout_rows())
        error = next(i for i in plan.verdict.errors if i.code is Code.CABLE_GROUP_PARTLY_CABLED)
        assert "LNK-0001" in error.message
        assert "cannot add the missing termination" in error.message
        assert all(planned.outcome == RowOutcome.BLOCKED for planned in plan.rows)

    def test_a_partly_cabled_group_writes_nothing_at_all(self, importer, netbox, topology):
        topology.front_ports["MPO2"]["cable"] = {"id": 9101}
        plan = validate(importer, netbox, breakout_rows())
        with pytest.raises(ApplyRefused):
            apply_plan(importer, netbox, plan)
        assert netbox.posted(CABLES) == []

    def test_a_group_whose_terminations_are_spread_over_two_cables_blocks(
        self, importer, netbox, topology
    ):
        for index, cable_id in ((1, 9201), (2, 9202), (3, 9203), (4, 9204)):
            topology.front_ports[f"MPO{index}"]["cable"] = {"id": cable_id}
        topology.interfaces["swp1"]["cable"] = {"id": 9201}

        plan = validate(importer, netbox, breakout_rows())
        assert any(i.code is Code.CABLE_GROUP_PARTLY_CABLED for i in plan.verdict.errors)

    def test_an_ungrouped_row_with_one_occupied_end_still_skips(self, importer, netbox, topology):
        """Requirement 9.9 is untouched: with one termination a side, a skip loses nothing."""
        topology.interfaces["swp1"]["cable"] = {"id": 9301}
        plan = validate(importer, netbox, [row()])
        assert plan.rows[0].outcome == RowOutcome.SKIP_EXISTS
        assert plan.verdict.errors == []

    def test_the_skip_message_says_group_when_it_is_one(self, importer, netbox, topology):
        cable = self.cable_the_group(importer, netbox, topology)
        second = validate(importer, netbox, breakout_rows())
        info = next(i for i in second.verdict.infos if i.code is Code.PORT_ALREADY_CABLED)
        assert "The group was skipped" in info.message
        assert str(cable["id"]) in info.related_url
