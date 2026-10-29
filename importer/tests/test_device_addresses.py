# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Device loopback and management addresses (D11, Requirements 8.13–8.26).

Every address here is documentation-range (RFC 5737 / 3849). Nothing is a real host.
"""

from typing import Any

import pytest

from importer.importers.base import RowOutcome
from importer.importers.devices import DeviceImporter
from importer.tests.fakes import FakeNetBox
from importer.tests.test_device_importer import (
    DEVICES,
    apply_plan,
    mounted,
    row,
    seed,
    validate,
)
from importer.validation import Code

INTERFACES = "dcim.interfaces"
TEMPLATES = "dcim.interface-templates"
IPS = "ipam.ip-addresses"

MGMT_V4 = "192.0.2.10/24"
MGMT_V6 = "2001:db8:1::10/64"
LOOP_V4 = "192.0.2.1/32"
LOOP_V6 = "2001:db8::1/128"


@pytest.fixture
def netbox():
    return FakeNetBox()


@pytest.fixture
def importer():
    return DeviceImporter()


def add_templates(
    netbox: FakeNetBox,
    device_type: dict[str, Any],
    *,
    mgmt: str = "eth0",
    loopback: str = "lo",
) -> None:
    netbox.add(
        TEMPLATES,
        device_type={"id": device_type["id"]},
        name=mgmt,
        type="1000base-t",
        mgmt_only=True,
    )
    netbox.add(
        TEMPLATES,
        device_type={"id": device_type["id"]},
        name=loopback,
        type="loopback",
        mgmt_only=False,
    )


def add_ifaces(
    netbox: FakeNetBox,
    device: dict[str, Any],
    *,
    mgmt: str = "eth0",
    loopback: str = "lo",
) -> tuple[dict[str, Any], dict[str, Any]]:
    eth = netbox.add(
        INTERFACES,
        device={"id": device["id"], "name": device["name"]},
        name=mgmt,
        type={"value": "1000base-t"},
        mgmt_only=True,
    )
    lo = netbox.add(
        INTERFACES,
        device={"id": device["id"], "name": device["name"]},
        name=loopback,
        type={"value": "loopback"},
        mgmt_only=False,
    )
    return eth, lo


def addressed(**overrides: str) -> dict[str, str]:
    return row(
        management_ip4=MGMT_V4,
        loopback_ip4=LOOP_V4,
        primary_address_v4=MGMT_V4,
        **overrides,
    )


# --- S2 / S3 --------------------------------------------------------------------------------


class TestAddressCells:
    def test_valid_families_are_accepted(self, importer, netbox):
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)
        plan = validate(
            importer,
            netbox,
            [
                row(
                    management_ip4=MGMT_V4,
                    management_ip6=MGMT_V6,
                    loopback_ip4=LOOP_V4,
                    loopback_ip6=LOOP_V6,
                )
            ],
        )
        assert plan.verdict.errors == []
        assert plan.verdict.is_applyable()

    def test_a_host_without_a_mask_is_rejected_not_defaulted(self, importer, netbox):
        seed(netbox)
        plan = validate(importer, netbox, [row(management_ip4="10.0.0.1")])
        error = next(i for i in plan.verdict.errors if i.code is Code.DEVICE_IP_INVALID)
        assert error.column == "management_ip4"
        assert "not defaulted" in error.message

    def test_v6_in_an_ip4_column_is_a_family_mismatch(self, importer, netbox):
        seed(netbox)
        plan = validate(importer, netbox, [row(management_ip4=LOOP_V6)])
        error = next(i for i in plan.verdict.errors if i.code is Code.DEVICE_IP_FAMILY_MISMATCH)
        assert error.column == "management_ip4"

    def test_v6_in_primary_address_v4_is_a_family_mismatch(self, importer, netbox):
        seed(netbox)
        plan = validate(importer, netbox, [row(primary_address_v4=LOOP_V6)])
        error = next(i for i in plan.verdict.errors if i.code is Code.DEVICE_IP_FAMILY_MISMATCH)
        assert error.column == "primary_address_v4"

    def test_the_same_address_in_two_rows_blocks(self, importer, netbox):
        seed(netbox)
        plan = validate(
            importer,
            netbox,
            [
                addressed(),
                row(
                    device_name="exa1-dh1-r01-leaf02",
                    bottom_ru="12",
                    management_ip4=MGMT_V4,
                    loopback_ip4="192.0.2.2/32",
                ),
            ],
        )
        error = next(i for i in plan.verdict.errors if i.code is Code.DUPLICATE_IP_IN_FILE)
        assert error.line == 3
        assert error.related_line == 2

    def test_the_same_address_in_two_columns_of_one_row_blocks(self, importer, netbox):
        seed(netbox)
        plan = validate(
            importer,
            netbox,
            [row(management_ip4=MGMT_V4, loopback_ip4=MGMT_V4)],
        )
        error = next(i for i in plan.verdict.errors if i.code is Code.DUPLICATE_IP_IN_FILE)
        assert error.column == "management_ip4"


# --- S4: interface resolution ---------------------------------------------------------------


class TestInterfaceResolution:
    def test_mgmt_only_resolves_arista_or_cumulus_names(self, importer, netbox):
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type, mgmt="Management1", loopback="Loopback0")
        plan = validate(importer, netbox, [row(management_ip4=MGMT_V4)])
        assert plan.verdict.errors == []
        assert "Management1" in plan.rows[0].resolved["management_ip4"]

        other_box = FakeNetBox()
        seeded = seed(other_box)
        add_templates(other_box, seeded.device_type, mgmt="eth0", loopback="lo")
        plan = validate(importer, other_box, [row(management_ip4=MGMT_V4)])
        assert "eth0" in plan.rows[0].resolved["management_ip4"]

    def test_two_mgmt_only_templates_are_ambiguous(self, importer, netbox):
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)
        netbox.add(
            TEMPLATES,
            device_type={"id": seeded.device_type["id"]},
            name="Management1",
            type="1000base-t",
            mgmt_only=True,
        )
        plan = validate(importer, netbox, [row(management_ip4=MGMT_V4)])
        error = next(i for i in plan.verdict.errors if i.code is Code.MGMT_INTERFACE_AMBIGUOUS)
        assert "eth0" in error.message and "Management1" in error.message

    def test_a_panel_type_with_no_role_interfaces_accepts_blank_ips(self, importer, netbox):
        """Requirement 8.14: patch panels share the sheet by leaving every address cell blank."""
        seed(netbox)
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.errors == []
        assert plan.rows[0].outcome == RowOutcome.CREATE
        assert all(action.endpoint != IPS for action in plan.rows[0].actions)

    def test_a_switch_with_templates_and_blank_ips_is_rejected(self, importer, netbox):
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)
        plan = validate(importer, netbox, [row()])
        error = next(i for i in plan.verdict.errors if i.code is Code.DEVICE_IP_REQUIRED)
        assert "at least one" in error.message

    def test_a_management_address_against_a_type_with_no_mgmt_blocks(self, importer, netbox):
        seed(netbox)
        plan = validate(importer, netbox, [row(management_ip4=MGMT_V4)])
        assert {i.code for i in plan.verdict.errors} & {
            Code.MGMT_INTERFACE_NOT_FOUND,
            Code.DEVICE_IP_NOT_APPLICABLE,
        }


# --- apply: create, remaining work, primary IP ----------------------------------------------


class TestAddressApply:
    def test_a_new_device_gets_addresses_then_primary_ip(self, importer, netbox):
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)
        plan = validate(importer, netbox, [addressed()])
        assert plan.verdict.is_applyable()
        report = apply_plan(importer, netbox, plan)
        assert report.aborted is False
        assert {post.endpoint for post in netbox.posts} >= {DEVICES, IPS}
        assert netbox.patches
        patch = netbox.patches[0].payloads[0]
        assert set(patch) <= {"id", "primary_ip4", "primary_ip6"}
        assert "primary_ip4" in patch
        device = netbox.stored(DEVICES)[0]
        assert device["primary_ip4"]["address"] == MGMT_V4
        addresses = {ip["address"] for ip in netbox.stored(IPS)}
        assert addresses == {MGMT_V4, LOOP_V4}

    def test_reupload_creates_nothing_when_addresses_already_match(self, importer, netbox):
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)
        plan = validate(importer, netbox, [addressed()])
        apply_plan(importer, netbox, plan)
        netbox.posts.clear()
        netbox.patches.clear()

        plan = validate(importer, netbox, [addressed()])
        assert plan.rows[0].outcome == RowOutcome.SKIP_EXISTS
        report = apply_plan(importer, netbox, plan)
        assert report.created_count == 0
        assert netbox.posts == []
        assert netbox.patches == []

    def test_an_existing_device_missing_ips_gets_them(self, importer, netbox):
        """Requirement 8.20: skip the device create, still do remaining address work."""
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)
        device = mounted(netbox, seeded)
        add_ifaces(netbox, device)

        plan = validate(importer, netbox, [addressed()])
        assert plan.rows[0].outcome == RowOutcome.CREATE
        assert all(
            action.endpoint != DEVICES or action.method == "patch"
            for action in plan.rows[0].actions
        )
        report = apply_plan(importer, netbox, plan)
        assert report.aborted is False
        assert netbox.posted(IPS)
        assert netbox.patches
        assert len(netbox.stored(DEVICES)) == 1

    def test_an_existing_device_missing_the_type_interfaces_gets_them(self, importer, netbox):
        """Devices created before the templates existed still receive the role ports."""
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)
        mounted(netbox, seeded)

        plan = validate(importer, netbox, [addressed()])
        assert plan.verdict.is_applyable()
        report = apply_plan(importer, netbox, plan)
        assert report.aborted is False
        names = {iface["name"] for iface in netbox.stored(INTERFACES)}
        assert {"eth0", "lo"} <= names
        assert netbox.posted(IPS)

    def test_an_address_held_by_another_device_blocks_with_zero_writes(self, importer, netbox):
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)
        other = mounted(netbox, seeded, name="exa1-dh1-r01-leaf09", position=20)
        eth, _ = add_ifaces(netbox, other)
        netbox.add(
            IPS,
            address=MGMT_V4,
            assigned_object_type="dcim.interface",
            assigned_object_id=eth["id"],
            assigned_object={
                "id": eth["id"],
                "name": "eth0",
                "device": {"id": other["id"], "name": other["name"]},
            },
        )
        plan = validate(importer, netbox, [addressed()])
        error = next(i for i in plan.verdict.errors if i.code is Code.IP_ALREADY_ASSIGNED)
        assert "leaf09" in error.message
        assert plan.verdict.is_applyable() is False

    def test_explicit_v6_and_v4_references_set_primary_per_family(self, importer, netbox):
        """Requirement 8.18: both explicit family references are applied."""
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)
        plan = validate(
            importer,
            netbox,
            [
                row(
                    management_ip6=MGMT_V6,
                    loopback_ip4=LOOP_V4,
                    primary_address_v4=LOOP_V4,
                    primary_address_v6=MGMT_V6,
                )
            ],
        )
        assert plan.rows[0].resolved["primary_ip4"] == LOOP_V4
        assert plan.rows[0].resolved["primary_ip6"] == MGMT_V6
        assert plan.rows[0].resolved["displayed_primary"] == "primary_ip4"
        apply_plan(importer, netbox, plan)
        patch = netbox.patches[0].payloads[0]
        assert set(patch) == {"id", "primary_ip4", "primary_ip6"}

    def test_a_v6_only_row_sets_only_primary_ip6(self, importer, netbox):
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)
        plan = validate(
            importer,
            netbox,
            [row(management_ip6=MGMT_V6, primary_address_v6=MGMT_V6)],
        )
        assert "primary_ip4" not in plan.rows[0].resolved
        assert plan.rows[0].resolved["displayed_primary"] == "primary_ip6"
        apply_plan(importer, netbox, plan)
        patch = netbox.patches[0].payloads[0]
        assert set(patch) == {"id", "primary_ip6"}

    def test_patch_actions_carry_only_primary_ip_fields(self, importer, netbox):
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)
        plan = validate(importer, netbox, [addressed()])
        patches = [action for action in plan.create_actions if action.method == "patch"]
        assert patches
        for action in patches:
            assert action.endpoint == DEVICES
            assert set(action.payload) <= {"id", "primary_ip4", "primary_ip6"}

    def test_blank_primary_columns_do_not_infer_a_primary(self, importer, netbox):
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)

        plan = validate(
            importer,
            netbox,
            [row(management_ip4=MGMT_V4, loopback_ip4=LOOP_V4)],
        )

        assert plan.verdict.is_applyable()
        assert not [action for action in plan.create_actions if action.method == "patch"]
        apply_plan(importer, netbox, plan)
        assert netbox.patches == []
        assert not netbox.stored(DEVICES)[0].get("primary_ip4")

    def test_existing_device_can_select_an_ip_on_any_existing_interface(self, importer, netbox):
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)
        device = mounted(netbox, seeded)
        add_ifaces(netbox, device)
        uplink = netbox.add(
            INTERFACES,
            device={"id": device["id"], "name": device["name"]},
            name="swp1",
            type={"value": "1000base-t"},
            mgmt_only=False,
        )
        existing_cidr = "198.51.100.10/24"
        existing_ip = netbox.add(
            IPS,
            address=existing_cidr,
            assigned_object_type="dcim.interface",
            assigned_object_id=uplink["id"],
            assigned_object={
                "id": uplink["id"],
                "name": "swp1",
                "device": {"id": device["id"], "name": device["name"]},
            },
        )

        plan = validate(
            importer,
            netbox,
            [
                row(
                    management_ip4=MGMT_V4,
                    primary_address_v4=existing_cidr,
                )
            ],
        )

        assert plan.verdict.is_applyable()
        assert "existing on device" in plan.rows[0].resolved["primary_address_v4"]
        apply_plan(importer, netbox, plan)
        assert netbox.patches[0].payloads[0]["primary_ip4"] == existing_ip["id"]

    def test_primary_reference_not_assigned_to_device_blocks(self, importer, netbox):
        seeded = seed(netbox)
        add_templates(netbox, seeded.device_type)
        device = mounted(netbox, seeded)
        add_ifaces(netbox, device)

        plan = validate(
            importer,
            netbox,
            [
                row(
                    management_ip4=MGMT_V4,
                    primary_address_v4="198.51.100.99/24",
                )
            ],
        )

        error = next(
            issue
            for issue in plan.verdict.errors
            if issue.code is Code.PRIMARY_IP_NOT_ON_DEVICE
        )
        assert error.column == "primary_address_v4"
        assert plan.verdict.is_applyable() is False
