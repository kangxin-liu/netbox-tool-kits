# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 14 — the device importer, one test per acceptance criterion.

Requirements 8.1–8.12, 10.1, 10.6, 10.7, 10.14, 11.2, 11.5, 11.5a, 11.5b, 11.6, 11.7, 12.1,
15.2, 16.6, 17.1.

Every value here is synthetic: an example region, example racks, an example manufacturer, and
device names built from them. Nothing resembles a real host, serial, or credential.
"""

import csv
import io
from dataclasses import dataclass
from typing import Any

import pytest
from django.test import override_settings

from importer.columns import columns_for
from importer.importers.base import ApplyRefused, RowOutcome
from importer.importers.devices import DeviceImporter
from importer.services.lookups import Lookups
from importer.services.sheets import parse_upload
from importer.tests.fakes import FakeNetBox
from importer.validation import Code

SITES = "dcim.sites"
RACKS = "dcim.racks"
MANUFACTURERS = "dcim.manufacturers"
DEVICE_TYPES = "dcim.device-types"
DEVICES = "dcim.devices"
ROLES = "dcim.device-roles"
TENANTS = "tenancy.tenants"

BASE_ROW = {
    "rack": "EXA1-DH1-R01",
    "rack_face": "front",
    "bottom_ru": "10",
    "ru_height": "2",
    "device_name": "exa1-dh1-r01-leaf01",
    "model": "Example-Switch-32C",
}


def sheet(rows: list[dict[str, str]], filename: str = "devices.csv"):
    """Build a real csv and parse it, so tests exercise the same path an upload takes."""
    specs = columns_for("devices")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([spec.label for spec in specs])
    for row_values in rows:
        writer.writerow([row_values.get(spec.key, "") for spec in specs])
    payload = buffer.getvalue().encode("utf-8-sig")
    return parse_upload(
        "devices",
        filename=filename,
        stream=io.BytesIO(payload),
        size=len(payload),
        content_type="text/csv",
    )


def row(**overrides: str) -> dict[str, str]:
    return {**BASE_ROW, **overrides}


@dataclass
class Seeded:
    """The prerequisites `BASE_ROW` assumes, already present in NetBox."""

    site: dict[str, Any]
    rack: dict[str, Any]
    manufacturer: dict[str, Any]
    device_type: dict[str, Any]
    role: dict[str, Any]


def seed(netbox, *, rack_height: int = 45, type_height: int = 2) -> Seeded:
    site = netbox.add(SITES, name="EXA1", slug="exa1")
    rack = netbox.add(
        RACKS,
        name="EXA1-DH1-R01",
        site={"id": site["id"], "name": "EXA1"},
        u_height=rack_height,
    )
    manufacturer = netbox.add(MANUFACTURERS, name="Example Networks", slug="example-networks")
    device_type = netbox.add(
        DEVICE_TYPES,
        model="Example-Switch-32C",
        slug="example-switch-32c",
        manufacturer={"id": manufacturer["id"], "name": "Example Networks"},
        u_height=type_height,
    )
    # `NETBOX_DEFAULT_DEVICE_ROLE` is `unassigned` in the test settings, so a row that names no
    # role falls back to this one (decision D2).
    role = netbox.add(ROLES, name="Unassigned", slug="unassigned")
    return Seeded(
        site=site,
        rack=rack,
        manufacturer=manufacturer,
        device_type=device_type,
        role=role,
    )


def mounted(netbox, seeded: Seeded, **overrides: Any) -> dict[str, Any]:
    """A device in the shape NetBox returns one: nested rack, face, position, and device type."""
    fields: dict[str, Any] = {
        "name": "exa1-dh1-r01-leaf01",
        "rack": {"id": seeded.rack["id"], "name": seeded.rack["name"]},
        "site": {"id": seeded.site["id"], "name": "EXA1"},
        "face": {"value": "front", "label": "Front"},
        "position": 10,
        "device_type": {
            "id": seeded.device_type["id"],
            "model": "Example-Switch-32C",
            "slug": "example-switch-32c",
            "u_height": 2,
        },
        "role": {"id": seeded.role["id"], "name": "Unassigned", "slug": "unassigned"},
        "status": {"value": "active", "label": "Active"},
    }
    fields.update(overrides)
    return netbox.add(DEVICES, **fields)


@pytest.fixture
def netbox():
    return FakeNetBox()


@pytest.fixture
def importer():
    return DeviceImporter()


def validate(importer, netbox, rows):
    return importer.validate(sheet(rows), Lookups(client=netbox))


def apply_plan(importer, netbox, plan):
    return importer.apply(plan, Lookups(client=netbox))


def device_payload(plan, index: int = 0) -> dict[str, Any]:
    return next(a for a in plan.rows[index].actions if a.endpoint == DEVICES).payload


# --- the happy path -------------------------------------------------------------------------


class TestPlanning:
    def test_one_row_plans_one_device_with_the_fields_netbox_4_6_wants(self, importer, netbox):
        """Requirements 8.3, 8.9: position is bottom_ru and the site comes from the rack."""
        seeded = seed(netbox)
        plan = validate(importer, netbox, [row()])

        assert plan.verdict.is_applyable() is True
        assert plan.verdict.stages_completed == ("S1", "S2", "S3", "S4", "S5")
        assert plan.rows[0].outcome == RowOutcome.CREATE
        assert device_payload(plan) == {
            "name": "exa1-dh1-r01-leaf01",
            "device_type": seeded.device_type["id"],
            "role": seeded.role["id"],
            "site": seeded.site["id"],
            "rack": seeded.rack["id"],
            "position": 10,
            "face": "front",
            "status": "active",
        }

    def test_nothing_is_written_during_validation(self, importer, netbox):
        """Requirement 10.1."""
        seed(netbox)
        validate(importer, netbox, [row(), row(device_name="exa1-dh1-r01-leaf02", bottom_ru="12")])
        assert netbox.posts == []

    def test_optional_free_text_is_submitted_only_when_the_sheet_supplies_it(
        self, importer, netbox
    ):
        """Requirement 8.9."""
        seed(netbox)
        netbox.add(TENANTS, name="Example Tenant", slug="example-tenant")
        plan = validate(
            importer,
            netbox,
            [
                row(
                    serial="EXAMPLE-SERIAL-0002",
                    asset_tag="EXAMPLE-ASSET-0002",
                    status="planned",
                    tenant="Example Tenant",
                )
            ],
        )
        payload = device_payload(plan)
        assert payload["serial"] == "EXAMPLE-SERIAL-0002"
        assert payload["asset_tag"] == "EXAMPLE-ASSET-0002"
        assert payload["status"] == "planned"
        assert payload["tenant"] == netbox.stored(TENANTS)[0]["id"]

    def test_the_plan_records_what_each_reference_resolved_to(self, importer, netbox):
        """Requirement 10.18."""
        seed(netbox)
        plan = validate(importer, netbox, [row()])
        assert plan.rows[0].resolved["rack"] == "EXA1-DH1-R01 (existing)"
        assert plan.rows[0].resolved["model"] == "Example-Switch-32C (existing)"
        assert plan.rows[0].resolved["device_role"] == "Unassigned (existing)"
        assert plan.rows[0].resolved["device_name"] == "exa1-dh1-r01-leaf01 (create)"


# --- S2: cells ------------------------------------------------------------------------------


class TestCellChecks:
    @pytest.mark.parametrize("column", ["rack", "rack_face", "bottom_ru", "device_name", "model"])
    def test_a_missing_required_value_blocks(self, importer, netbox, column):
        seed(netbox)
        plan = validate(importer, netbox, [row(**{column: ""})])
        assert plan.verdict.is_applyable() is False
        assert Code.REQUIRED_VALUE_BLANK in {issue.code for issue in plan.verdict.errors}

    def test_a_rack_face_netbox_does_not_accept_is_rejected(self, importer, netbox):
        """Requirement 8.2: front or rear, and nothing else."""
        seed(netbox)
        plan = validate(importer, netbox, [row(rack_face="side")])
        error = next(i for i in plan.verdict.errors if i.code is Code.VALUE_NOT_IN_CHOICES)
        assert error.column == "rack_face"
        assert "front" in error.expected and "rear" in error.expected

    @pytest.mark.parametrize("face", ["FRONT", "Rear", " front "])
    def test_a_rack_face_is_accepted_case_insensitively(self, importer, netbox, face):
        """Requirement 8.2."""
        seed(netbox)
        plan = validate(importer, netbox, [row(rack_face=face)])
        assert plan.verdict.errors == []
        assert device_payload(plan)["face"] == face.strip().casefold()

    @pytest.mark.parametrize("bottom", ["0", "-2", "abc", "10.5"])
    def test_a_bottom_ru_that_is_not_a_positive_integer_blocks(self, importer, netbox, bottom):
        """Requirement 8.4."""
        seed(netbox)
        plan = validate(importer, netbox, [row(bottom_ru=bottom)])
        assert {i.code for i in plan.verdict.errors} == {Code.NOT_A_POSITIVE_INTEGER}

    def test_a_status_outside_the_choices_netbox_reports_blocks(self, importer, netbox):
        """Requirement 8.9: the choices come from the API, not a hardcoded list."""
        seed(netbox)
        plan = validate(importer, netbox, [row(status="retired")])
        error = next(i for i in plan.verdict.errors if i.code is Code.VALUE_NOT_IN_CHOICES)
        assert "retired" in error.message
        assert "decommissioning" in error.expected

    def test_a_malformed_rack_name_blocks_with_the_expected_pattern(self, importer, netbox):
        """Requirement 6.2: the rack name is the join key between the sheets."""
        seed(netbox)
        plan = validate(importer, netbox, [row(rack="R01")])
        error = next(i for i in plan.verdict.errors if i.code is Code.RACK_NAME_MALFORMED)
        assert "<datacenter_slug>-<datahall_slug>-<rack>" in error.expected

    def test_every_problem_in_the_stage_is_reported_not_just_the_first(self, importer, netbox):
        """Requirement 10.3."""
        seed(netbox)
        plan = validate(
            importer,
            netbox,
            [row(bottom_ru="0"), row(device_name="exa1-dh1-r01-leaf02", rack_face="sideways")],
        )
        assert len(plan.verdict.errors) >= 2
        assert {issue.line for issue in plan.verdict.errors} == {2, 3}


# --- S3: consistency within the file --------------------------------------------------------


class TestConsistency:
    def test_a_duplicate_device_name_rejects_the_later_row_and_names_the_earlier(
        self, importer, netbox
    ):
        """Requirements 8.11, 16.6."""
        seed(netbox)
        plan = validate(importer, netbox, [row(), row(bottom_ru="20")])
        error = next(i for i in plan.verdict.errors if i.code is Code.DUPLICATE_DEVICE_IN_FILE)
        assert error.line == 3
        assert error.related_line == 2
        assert "line 2" in error.message

    def test_a_duplicate_device_name_is_detected_regardless_of_casing(self, importer, netbox):
        seed(netbox)
        plan = validate(
            importer, netbox, [row(), row(device_name="EXA1-DH1-R01-LEAF01", bottom_ru="20")]
        )
        assert any(i.code is Code.DUPLICATE_DEVICE_IN_FILE for i in plan.verdict.errors)

    def test_two_rows_claiming_the_same_units_reject_the_later_row(self, importer, netbox):
        """Requirements 8.5, 16.6: the conflict names both lines and both devices."""
        seed(netbox)
        plan = validate(
            importer,
            netbox,
            [row(), row(device_name="exa1-dh1-r01-leaf02", bottom_ru="11")],
        )
        error = next(i for i in plan.verdict.errors if i.code is Code.RU_RANGE_OVERLAP)
        assert error.line == 3
        assert error.related_line == 2
        assert "U11-U12" in error.message and "U10-U11" in error.message
        assert "exa1-dh1-r01-leaf01" in error.message

    def test_the_same_units_on_the_other_face_are_not_an_overlap(self, importer, netbox):
        """Requirement 8.5: a rack has two independent faces."""
        seed(netbox)
        plan = validate(
            importer,
            netbox,
            [row(), row(device_name="exa1-dh1-r01-leaf02", rack_face="rear")],
        )
        assert plan.verdict.errors == []

    def test_the_same_units_in_another_rack_are_not_an_overlap(self, importer, netbox):
        seeded = seed(netbox)
        netbox.add(
            RACKS,
            name="EXA1-DH1-R02",
            site={"id": seeded.site["id"], "name": "EXA1"},
            u_height=45,
        )
        plan = validate(
            importer,
            netbox,
            [row(), row(rack="EXA1-DH1-R02", device_name="exa1-dh1-r02-leaf01")],
        )
        assert plan.verdict.errors == []

    def test_two_rows_carrying_the_same_asset_tag_reject_the_later_row(self, importer, netbox):
        seed(netbox)
        plan = validate(
            importer,
            netbox,
            [
                row(asset_tag="EXAMPLE-ASSET-0001"),
                row(
                    device_name="exa1-dh1-r01-leaf02",
                    bottom_ru="20",
                    asset_tag="example-asset-0001",
                ),
            ],
        )
        error = next(i for i in plan.verdict.errors if i.code is Code.DUPLICATE_ASSET_TAG_IN_FILE)
        assert error.line == 3
        assert error.related_line == 2


# --- S4: the rack and the units it has free -------------------------------------------------


class TestRackPlacement:
    def test_a_rack_that_does_not_exist_is_rejected_and_never_created(self, importer, netbox):
        """Requirements 8.1, 17.1."""
        seed(netbox)
        plan = validate(importer, netbox, [row(rack="EXA1-DH1-R99")])
        error = next(i for i in plan.verdict.errors if i.code is Code.RACK_NOT_FOUND)
        assert "never creates racks" in error.message
        assert plan.rows[0].outcome == RowOutcome.BLOCKED
        assert netbox.posts == []
        assert len(netbox.stored(RACKS)) == 1

    def test_a_rack_is_matched_case_insensitively(self, importer, netbox):
        """Requirement 6.4."""
        seed(netbox)
        plan = validate(importer, netbox, [row(rack="exa1-dh1-r01")])
        assert plan.verdict.errors == []

    def test_a_range_reaching_past_the_top_of_the_rack_states_the_racks_height(
        self, importer, netbox
    ):
        """Requirement 8.4."""
        seed(netbox, rack_height=45)
        plan = validate(importer, netbox, [row(bottom_ru="45")])
        error = next(i for i in plan.verdict.errors if i.code is Code.RU_RANGE_EXCEEDS_RACK)
        assert "45U high" in error.message
        assert "U45-U46" in error.message
        assert plan.rows[0].outcome == RowOutcome.BLOCKED

    def test_a_device_ending_exactly_at_the_top_of_the_rack_fits(self, importer, netbox):
        """Requirement 8.4: bottom_ru + ru_height - 1 may equal the rack height."""
        seed(netbox, rack_height=45)
        plan = validate(importer, netbox, [row(bottom_ru="44")])
        assert plan.verdict.errors == []

    def test_overlap_with_a_device_already_mounted_on_that_face_identifies_the_occupant(
        self, importer, netbox
    ):
        """Requirement 8.5."""
        seeded = seed(netbox)
        occupant = mounted(netbox, seeded, name="exa1-dh1-r01-leaf09", position=11)
        plan = validate(importer, netbox, [row()])

        error = next(i for i in plan.verdict.errors if i.code is Code.RU_RANGE_OVERLAP)
        assert "exa1-dh1-r01-leaf09" in error.message
        assert "U11-U12" in error.message
        assert error.related_url.endswith(f"/dcim/devices/{occupant['id']}/")
        assert plan.rows[0].outcome == RowOutcome.BLOCKED

    def test_a_device_on_the_other_face_of_the_same_units_is_not_an_overlap(self, importer, netbox):
        """Requirement 8.5."""
        seeded = seed(netbox)
        mounted(
            netbox,
            seeded,
            name="exa1-dh1-r01-leaf09",
            face={"value": "rear", "label": "Rear"},
        )
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.errors == []

    def test_an_occupant_of_one_rack_does_not_block_the_same_units_in_another(
        self, importer, netbox
    ):
        """Requirement 8.5: the occupancy check is scoped to the rack the row names."""
        seeded = seed(netbox)
        other = netbox.add(
            RACKS, name="EXA1-DH1-R02", site={"id": seeded.site["id"], "name": "EXA1"}, u_height=45
        )
        mounted(
            netbox,
            seeded,
            name="exa1-dh1-r02-leaf09",
            rack={"id": other["id"], "name": other["name"]},
        )
        plan = validate(
            importer,
            netbox,
            [row(), row(rack="EXA1-DH1-R02", device_name="exa1-dh1-r02-leaf01")],
        )

        overlaps = [i for i in plan.verdict.errors if i.code is Code.RU_RANGE_OVERLAP]
        # Only the row aimed at the rack the occupant is actually in.
        assert [issue.line for issue in overlaps] == [3]
        assert "exa1-dh1-r02-leaf09" in overlaps[0].message


# --- S4: the device type --------------------------------------------------------------------


class TestDeviceType:
    def test_a_ru_height_disagreeing_with_the_device_type_reports_both_heights(
        self, importer, netbox
    ):
        """Requirement 8.6: NetBox draws the elevation from the device type."""
        seed(netbox, type_height=2)
        plan = validate(importer, netbox, [row(ru_height="4")])
        error = next(i for i in plan.verdict.errors if i.code is Code.RU_HEIGHT_DISAGREES_WITH_TYPE)
        assert error.value == "4" and error.expected == "2"
        assert "Example-Switch-32C" in error.message

    def test_an_unknown_model_is_rejected_when_autocreate_is_off(self, importer, netbox):
        """Requirements 8.7, 17.1, decision D4: off by default."""
        seed(netbox)
        plan = validate(importer, netbox, [row(model="Example-Switch-64C")])
        error = next(i for i in plan.verdict.errors if i.code is Code.DEVICE_TYPE_NOT_FOUND)
        assert "Example-Switch-64C" in error.message
        assert "NETBOX_AUTOCREATE_DEVICE_TYPES" in error.message
        assert plan.rows[0].actions == []
        assert [t["model"] for t in netbox.stored(DEVICE_TYPES)] == ["Example-Switch-32C"]

    @override_settings(NETBOX_AUTOCREATE_DEVICE_TYPES=True)
    def test_autocreate_plans_the_manufacturer_and_the_type_and_warns_about_it(
        self, importer, netbox
    ):
        """Requirement 8.7 with the flag on: the operator has to look at the row first."""
        seed(netbox)
        plan = validate(
            importer,
            netbox,
            [row(model="Example-Router-8P", manufacturer="Example Routers", ru_height="3")],
        )

        assert plan.verdict.errors == []
        warning = next(
            i for i in plan.verdict.warnings if i.code is Code.DEVICE_TYPE_WILL_BE_CREATED
        )
        assert "will create it" in warning.message
        assert "3U" in warning.message
        # Requirement 10.9: a warning is not a free pass.
        assert plan.verdict.is_applyable() is False
        plan.verdict.acknowledge()
        assert plan.verdict.is_applyable() is True

        endpoints = [action.endpoint for action in plan.rows[0].actions]
        assert endpoints == [MANUFACTURERS, DEVICE_TYPES, DEVICES]
        created_type = plan.rows[0].actions[1].payload
        assert created_type["model"] == "Example-Router-8P"
        assert created_type["slug"] == "example-router-8p"
        assert created_type["u_height"] == 3

    @override_settings(NETBOX_AUTOCREATE_DEVICE_TYPES=True)
    def test_autocreate_writes_the_type_before_the_device_and_links_them(self, importer, netbox):
        """The device payload carries a `Ref` until the type it points at has been posted."""
        seed(netbox)
        plan = validate(
            importer,
            netbox,
            [row(model="Example-Router-8P", manufacturer="Example Routers", ru_height="3")],
        )
        plan.verdict.acknowledge()
        report = apply_plan(importer, netbox, plan)

        assert [post.endpoint for post in netbox.posts] == [
            MANUFACTURERS,
            DEVICE_TYPES,
            DEVICES,
        ]
        makers = netbox.stored(MANUFACTURERS)
        types = netbox.stored(DEVICE_TYPES)
        created_maker = next(m for m in makers if m["slug"] == "example-routers")
        created_type = next(t for t in types if t["slug"] == "example-router-8p")
        device = netbox.stored(DEVICES)[0]
        assert created_type["manufacturer"] == created_maker["id"]
        assert device["device_type"]["id"] == created_type["id"]
        assert report.created_count == 3

    @override_settings(NETBOX_AUTOCREATE_DEVICE_TYPES=True)
    def test_autocreate_reuses_a_manufacturer_that_already_exists(self, importer, netbox):
        seeded = seed(netbox)
        plan = validate(
            importer,
            netbox,
            [row(model="Example-Router-8P", manufacturer="Example Networks", ru_height="3")],
        )
        endpoints = [action.endpoint for action in plan.rows[0].actions]
        assert endpoints == [DEVICE_TYPES, DEVICES]
        assert plan.rows[0].actions[0].payload["manufacturer"] == seeded.manufacturer["id"]

    @override_settings(NETBOX_AUTOCREATE_DEVICE_TYPES=True)
    def test_autocreate_without_a_manufacturer_rejects_the_row_explaining_why(
        self, importer, netbox
    ):
        """Requirement 8.7: NetBox requires a manufacturer on every device type."""
        seed(netbox)
        plan = validate(importer, netbox, [row(model="Example-Router-8P")])
        error = next(
            i for i in plan.verdict.errors if i.code is Code.MANUFACTURER_REQUIRED_FOR_AUTOCREATE
        )
        assert error.column == "manufacturer"
        assert "requires a manufacturer" in error.message
        assert plan.rows[0].actions == []

    @override_settings(NETBOX_AUTOCREATE_DEVICE_TYPES=True)
    def test_two_rows_naming_the_same_new_model_create_it_once(self, importer, netbox):
        seed(netbox)
        rows = [
            row(model="Example-Router-8P", manufacturer="Example Routers", ru_height="3"),
            row(
                model="Example-Router-8P",
                manufacturer="Example Routers",
                ru_height="3",
                device_name="exa1-dh1-r01-rtr02",
                bottom_ru="20",
            ),
        ]
        plan = validate(importer, netbox, rows)
        plan.verdict.acknowledge()
        apply_plan(importer, netbox, plan)

        assert [post.size for post in netbox.posts] == [1, 1, 2]
        assert len(netbox.stored(DEVICE_TYPES)) == 2  # the seeded one plus the new one


# --- S4: role and tenant ---------------------------------------------------------------------


class TestRoleAndTenant:
    def test_the_configured_default_role_is_used_when_the_column_is_blank(self, importer, netbox):
        """Requirement 8.8, decision D2."""
        seeded = seed(netbox)
        plan = validate(importer, netbox, [row()])
        assert device_payload(plan)["role"] == seeded.role["id"]

    def test_a_role_named_by_the_row_overrides_the_default(self, importer, netbox):
        """Requirement 8.8."""
        seed(netbox)
        leaf = netbox.add(ROLES, name="Leaf Switch", slug="leaf-switch")
        plan = validate(importer, netbox, [row(device_role="Leaf Switch")])
        assert device_payload(plan)["role"] == leaf["id"]

    def test_a_role_may_be_named_by_its_slug(self, importer, netbox):
        seed(netbox)
        leaf = netbox.add(ROLES, name="Leaf Switch", slug="leaf-switch")
        plan = validate(importer, netbox, [row(device_role="leaf-switch")])
        assert device_payload(plan)["role"] == leaf["id"]

    def test_a_row_naming_a_role_that_does_not_exist_is_rejected(self, importer, netbox):
        """Requirements 8.8, 17.1."""
        seed(netbox)
        plan = validate(importer, netbox, [row(device_role="Spine Switch")])
        error = next(i for i in plan.verdict.errors if i.code is Code.DEVICE_ROLE_NOT_FOUND)
        assert "never creates roles" in error.message
        assert "this row" in error.message

    def test_a_default_role_that_does_not_resolve_rejects_the_row_naming_the_setting(
        self, importer, netbox
    ):
        """Requirements 8.8, 17.3: the misconfiguration is named, not just the missing role."""
        seed(netbox)
        netbox.objects[ROLES] = []
        plan = validate(importer, netbox, [row()])
        error = next(i for i in plan.verdict.errors if i.code is Code.DEVICE_ROLE_NOT_FOUND)
        assert "NETBOX_DEFAULT_DEVICE_ROLE" in error.message

    @override_settings(NETBOX_DEFAULT_DEVICE_ROLE="")
    def test_no_role_and_no_default_rejects_the_row(self, importer, netbox):
        """Requirement 8.8: NetBox requires a role, so there is nothing to guess."""
        seed(netbox)
        plan = validate(importer, netbox, [row()])
        error = next(i for i in plan.verdict.errors if i.code is Code.DEVICE_ROLE_NOT_FOUND)
        assert "not configured" in error.message

    def test_a_tenant_that_does_not_exist_blocks_and_is_never_created(self, importer, netbox):
        """Requirement 17.1."""
        seed(netbox)
        plan = validate(importer, netbox, [row(tenant="Example Tenant")])
        error = next(i for i in plan.verdict.errors if i.code is Code.TENANT_NOT_FOUND)
        assert "never creates" in error.message
        assert netbox.stored(TENANTS) == []


# --- S4: what already exists -----------------------------------------------------------------


class TestIdempotency:
    def test_an_existing_device_is_skipped_rather_than_recreated(self, importer, netbox):
        """Requirements 8.10, 11.2: never a duplicate, never a rename."""
        seeded = seed(netbox)
        mounted(netbox, seeded)

        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable() is True
        assert plan.rows[0].outcome == RowOutcome.SKIP_EXISTS
        assert plan.rows[0].actions == []
        info = next(i for i in plan.verdict.infos if i.code is Code.ALREADY_EXISTS)
        assert "nothing about it will be changed" in info.message

        report = apply_plan(importer, netbox, plan)
        assert report.created_count == 0
        assert report.skipped_count == 1
        assert netbox.posts == []

    def test_the_same_sheet_uploaded_twice_reports_every_row_as_already_present(
        self, importer, netbox
    ):
        """Requirement 11.7."""
        seeded = seed(netbox)
        mounted(netbox, seeded)
        mounted(netbox, seeded, name="exa1-dh1-r01-leaf02", position=12)

        rows = [row(), row(device_name="exa1-dh1-r01-leaf02", bottom_ru="12")]
        plan = validate(importer, netbox, rows)
        assert plan.verdict.errors == []
        assert [planned.outcome for planned in plan.rows] == [RowOutcome.SKIP_EXISTS] * 2

        report = apply_plan(importer, netbox, plan)
        assert report.created_count == 0
        assert netbox.posts == []

    def test_a_device_found_in_a_different_rack_blocks_rather_than_being_moved(
        self, importer, netbox
    ):
        """Requirements 11.3, 11.5, decision D8: a device's rack is consequential."""
        seeded = seed(netbox)
        other = netbox.add(
            RACKS, name="EXA1-DH1-R02", site={"id": seeded.site["id"], "name": "EXA1"}, u_height=45
        )
        mounted(netbox, seeded, rack={"id": other["id"], "name": other["name"]})

        plan = validate(importer, netbox, [row()])
        error = next(
            i
            for i in plan.verdict.errors
            if i.code is Code.EXISTS_BUT_DIFFERS and i.column == "rack"
        )
        assert error.value == "EXA1-DH1-R01" and error.expected == "EXA1-DH1-R02"
        assert plan.rows[0].outcome == RowOutcome.BLOCKED
        assert netbox.posts == []

    def test_a_device_found_at_a_different_position_blocks(self, importer, netbox):
        """Requirement 11.5: position is consequential."""
        seeded = seed(netbox)
        mounted(netbox, seeded, position=20)
        plan = validate(importer, netbox, [row()])
        error = next(
            i
            for i in plan.verdict.errors
            if i.code is Code.EXISTS_BUT_DIFFERS and i.column == "bottom_ru"
        )
        assert error.value == "10" and error.expected == "20"

    def test_a_device_found_on_the_other_face_blocks(self, importer, netbox):
        """Requirement 11.5: face is consequential."""
        seeded = seed(netbox)
        mounted(netbox, seeded, face={"value": "rear", "label": "Rear"})
        plan = validate(importer, netbox, [row()])
        assert any(
            i.code is Code.EXISTS_BUT_DIFFERS and i.column == "rack_face"
            for i in plan.verdict.errors
        )

    def test_a_serial_that_differs_only_warns(self, importer, netbox):
        """Requirement 11.5a, decision D8: nothing downstream reads a serial."""
        seeded = seed(netbox)
        mounted(netbox, seeded, serial="EXAMPLE-SERIAL-0001")
        plan = validate(importer, netbox, [row(serial="EXAMPLE-SERIAL-0002")])

        assert plan.verdict.errors == []
        assert len(plan.verdict.warnings) == 1
        assert plan.verdict.is_applyable() is False
        plan.verdict.acknowledge()
        assert plan.verdict.is_applyable() is True

    def test_a_presentation_difference_is_not_a_conflict(self, importer, netbox):
        """Requirement 11.5b."""
        seeded = seed(netbox)
        mounted(netbox, seeded, serial="example  serial  0001")
        plan = validate(importer, netbox, [row(serial="Example serial 0001")])
        assert plan.verdict.warnings == []

    def test_an_asset_tag_held_by_another_device_blocks_and_identifies_the_holder(
        self, importer, netbox
    ):
        """Requirement 11.6: a collision, not a difference in bookkeeping."""
        seeded = seed(netbox)
        holder = mounted(
            netbox,
            seeded,
            name="exa1-dh1-r01-leaf09",
            position=20,
            asset_tag="EXAMPLE-ASSET-0001",
        )
        plan = validate(importer, netbox, [row(asset_tag="EXAMPLE-ASSET-0001")])

        error = next(i for i in plan.verdict.errors if i.code is Code.UNIQUE_FIELD_TAKEN)
        assert "exa1-dh1-r01-leaf09" in error.message
        assert error.related_url.endswith(f"/dcim/devices/{holder['id']}/")
        assert plan.rows[0].outcome == RowOutcome.BLOCKED

    def test_an_asset_tag_recorded_differently_on_the_matched_device_only_warns(
        self, importer, netbox
    ):
        """Requirement 11.6: on the matched object it is descriptive, so it warns."""
        seeded = seed(netbox)
        mounted(netbox, seeded, asset_tag="EXAMPLE-ASSET-0001")
        plan = validate(importer, netbox, [row(asset_tag="EXAMPLE-ASSET-0002")])

        assert plan.verdict.errors == []
        warning = next(i for i in plan.verdict.warnings if i.column == "asset_tag")
        assert warning.expected == "EXAMPLE-ASSET-0001"

    def test_an_asset_tag_held_by_another_device_outranks_the_descriptive_warning(
        self, importer, netbox
    ):
        """Requirement 11.6: one precise error rather than that error and a vaguer warning."""
        seeded = seed(netbox)
        mounted(netbox, seeded, asset_tag="EXAMPLE-ASSET-0001")
        mounted(
            netbox,
            seeded,
            name="exa1-dh1-r01-leaf09",
            position=20,
            asset_tag="EXAMPLE-ASSET-0002",
        )
        plan = validate(importer, netbox, [row(asset_tag="EXAMPLE-ASSET-0002")])

        assert [i.code for i in plan.verdict.errors] == [Code.UNIQUE_FIELD_TAKEN]
        assert [i for i in plan.verdict.warnings if i.column == "asset_tag"] == []


# --- S5 and the request budget ----------------------------------------------------------------


class TestFeasibilityAndBudget:
    @override_settings(MAX_APPLY_ROWS=3)
    def test_a_file_over_the_row_limit_blocks_with_the_limit_named(self, importer, netbox):
        """Requirement 15.3."""
        seed(netbox)
        rows = [
            row(device_name=f"exa1-dh1-r01-leaf{index:02d}", bottom_ru=str(index * 2))
            for index in range(1, 6)
        ]
        plan = validate(importer, netbox, rows)
        error = next(i for i in plan.verdict.errors if i.code is Code.ROW_COUNT_OVER_LIMIT)
        assert "3-row limit" in error.message
        assert plan.verdict.is_applyable() is False

    def test_validation_requests_do_not_grow_with_the_row_count(self):
        """Requirement 15.2: bounded lookups, however long the sheet is."""

        def budget(rows: list[dict[str, str]]) -> int:
            netbox = FakeNetBox()
            seeded = seed(netbox)
            netbox.add(
                DEVICE_TYPES, model="Example-Server-1U", slug="example-server-1u", u_height=1
            )
            for index in range(2, 7):
                netbox.add(
                    RACKS,
                    name=f"EXA1-DH1-R{index:02d}",
                    site={"id": seeded.site["id"], "name": "EXA1"},
                    u_height=45,
                )
            DeviceImporter().validate(sheet(rows), Lookups(client=netbox))
            return netbox.request_count

        one_row = budget([row(model="Example-Server-1U", ru_height="1")])
        many = budget(
            [
                row(
                    rack=f"EXA1-DH1-R{1 + index // 40:02d}",
                    bottom_ru=str(1 + index % 40),
                    ru_height="1",
                    model="Example-Server-1U",
                    device_name=f"exa1-dh1-srv{index:03d}",
                )
                for index in range(200)
            ]
        )
        assert many <= one_row + 2


# --- apply ------------------------------------------------------------------------------------


class TestApply:
    def test_creates_are_grouped_into_one_post_per_endpoint(self, importer, netbox):
        """Decision D7: a list POST is transactional, so devices apply all-or-nothing."""
        seed(netbox)
        rows = [
            row(device_name=f"exa1-dh1-r01-leaf{index:02d}", bottom_ru=str(index * 2))
            for index in range(1, 5)
        ]
        plan = validate(importer, netbox, rows)
        apply_plan(importer, netbox, plan)

        assert [post.endpoint for post in netbox.posts] == [DEVICES]
        assert [post.size for post in netbox.posts] == [4]

    def test_devices_are_created_in_sheet_order(self, importer, netbox):
        """Requirement 8.12: the report lines up with the sheet."""
        seed(netbox)
        names = [f"exa1-dh1-r01-leaf{index:02d}" for index in range(1, 5)]
        rows = [
            row(device_name=name, bottom_ru=str(index * 2))
            for index, name in enumerate(names, start=1)
        ]
        plan = validate(importer, netbox, rows)
        apply_plan(importer, netbox, plan)

        assert [payload["name"] for payload in netbox.posted(DEVICES)[0].payloads] == names
        assert [record["name"] for record in netbox.stored(DEVICES)] == names

    def test_the_report_lists_the_created_device_with_its_line_and_a_link(self, importer, netbox):
        """Requirement 12.1."""
        seed(netbox)
        plan = validate(importer, netbox, [row()])
        report = apply_plan(importer, netbox, plan)

        result = next(r for r in report.results if r.object_kind == "device")
        assert result.line == 2
        assert result.object_name == "exa1-dh1-r01-leaf01"
        assert result.url.endswith(f"/dcim/devices/{result.object_id}/")
        assert report.headline.startswith("Applied — 1 created")


class TestApplyGate:
    def test_a_blocked_plan_cannot_be_applied_even_directly(self, importer, netbox):
        """Requirement 10.6: a direct POST is refused with an explanation."""
        seed(netbox)
        plan = validate(importer, netbox, [row(rack="EXA1-DH1-R99")])
        with pytest.raises(ApplyRefused) as raised:
            apply_plan(importer, netbox, plan)
        assert "error" in raised.value.reason
        assert netbox.posts == []

    def test_one_bad_row_makes_the_whole_file_unapplyable(self, importer, netbox):
        """Requirement 10.7: not even the valid rows, and not even on request."""
        seed(netbox)
        plan = validate(
            importer,
            netbox,
            [row(), row(device_name="exa1-dh1-r01-leaf02", bottom_ru="0")],
        )
        assert plan.verdict.errors
        assert all(planned.outcome == RowOutcome.BLOCKED for planned in plan.rows)
        assert plan.verdict.create_count == 0
        assert "no partial apply" in plan.verdict.refusal_reason()

    def test_a_rack_resized_since_validation_aborts_before_writing(self, importer, netbox):
        """Requirement 10.14: S4 is re-run immediately before the first write."""
        seeded = seed(netbox)
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable()

        seeded.rack["u_height"] = 42

        report = apply_plan(importer, netbox, plan)
        assert report.aborted is True
        assert "changed after this file was validated" in report.abort_reason
        assert "Nothing was written" in report.abort_reason
        assert "u_height=42" in report.abort_reason
        assert netbox.posts == []

    def test_a_device_racked_since_validation_aborts_before_writing(self, importer, netbox):
        """Requirement 10.14: someone else took the units between validate and apply."""
        seeded = seed(netbox)
        plan = validate(importer, netbox, [row()])
        mounted(netbox, seeded, name="exa1-dh1-r01-leaf09")

        report = apply_plan(importer, netbox, plan)
        assert report.aborted is True
        assert netbox.posts == []
