# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""D16 hierarchy contract: prepared site/parent, direct child location, then rack."""

import csv
import io

import pytest
from django.test import override_settings

from importer.columns import columns_for
from importer.importers.base import ApplyRefused, Ref, RowOutcome
from importer.importers.hierarchy import HierarchyImporter
from importer.services.lookups import Lookups
from importer.services.sheets import SheetRejected, parse_upload
from importer.tests.fakes import FakeNetBox
from importer.validation import Code, Severity

SITES = "dcim.sites"
LOCATIONS = "dcim.locations"
RACKS = "dcim.racks"
RACK_ROLES = "dcim.rack-roles"
TENANTS = "tenancy.tenants"

BASE_ROW = {
    "site_slug": "exa",
    "parent_location_slug": "exa01",
    "datahall": "EXA01-MMR1",
    "datahall_slug": "exa01_mmr1",
    "rack": "EXA01-MMR1-RK0001",
    "rack_u_height": "45",
}


def row(**overrides: str) -> dict[str, str]:
    return {**BASE_ROW, **overrides}


def sheet(rows: list[dict[str, str]], filename: str = "hierarchy.csv"):
    specs = columns_for("hierarchy")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([spec.label for spec in specs])
    for values in rows:
        writer.writerow([values.get(spec.key, "") for spec in specs])
    payload = buffer.getvalue().encode("utf-8-sig")
    return parse_upload(
        "hierarchy",
        filename=filename,
        stream=io.BytesIO(payload),
        size=len(payload),
        content_type="text/csv",
    )


def legacy_sheet():
    payload = (
        b"location,datacenter,datahall,rack,rack_u_height\r\n"
        b"Example Region,EXA1,DH1,EXA1-DH1-R01,45\r\n"
    )
    return parse_upload(
        "hierarchy",
        filename="legacy.csv",
        stream=io.BytesIO(payload),
        size=len(payload),
        content_type="text/csv",
    )


def seed_prerequisites(netbox: FakeNetBox, *, site_slug: str = "exa"):
    site = netbox.add(SITES, name="Example Site", slug=site_slug)
    parent = netbox.add(
        LOCATIONS,
        name="EXA01",
        slug="exa01",
        site={"id": site["id"], "name": site["name"], "slug": site["slug"]},
        parent=None,
    )
    return site, parent


def seed_child(netbox: FakeNetBox, site: dict, parent: dict, **overrides):
    return netbox.add(
        LOCATIONS,
        name=overrides.pop("name", "EXA01-MMR1"),
        slug=overrides.pop("slug", "exa01_mmr1"),
        site={"id": site["id"], "name": site["name"], "slug": site["slug"]},
        parent={"id": parent["id"], "name": parent["name"], "slug": parent["slug"]},
        **overrides,
    )


def seed_rack(netbox: FakeNetBox, site: dict, child: dict, **overrides):
    return netbox.add(
        RACKS,
        name=overrides.pop("name", "EXA01-MMR1-RK0001"),
        site={"id": site["id"], "name": site["name"], "slug": site["slug"]},
        location={"id": child["id"], "name": child["name"], "slug": child["slug"]},
        u_height=overrides.pop("u_height", 45),
        width=overrides.pop("width", {"value": 19}),
        status=overrides.pop("status", {"value": "active"}),
        **overrides,
    )


@pytest.fixture
def netbox():
    return FakeNetBox()


@pytest.fixture
def importer():
    return HierarchyImporter()


def validate(importer, netbox, rows):
    return importer.validate(sheet(rows), Lookups(client=netbox))


class TestPrerequisites:
    def test_missing_site_blocks_without_planning_creates(self, importer, netbox):
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable() is False
        assert Code.SITE_PREREQUISITE_MISSING in {issue.code for issue in plan.verdict.issues}
        assert plan.create_actions == []

    def test_missing_parent_blocks(self, importer, netbox):
        netbox.add(SITES, name="Example Site", slug="exa")
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable() is False
        assert any(issue.column == "parent_location_slug" for issue in plan.verdict.errors)

    def test_parent_with_same_slug_under_another_site_blocks(self, importer, netbox):
        wanted = netbox.add(SITES, name="Example Site", slug="exa")
        other = netbox.add(SITES, name="Other Site", slug="other")
        netbox.add(
            LOCATIONS,
            name="EXA01",
            slug="exa01",
            site={"id": other["id"], "name": other["name"]},
            parent=None,
        )
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable() is False
        assert wanted["id"] != other["id"]
        assert "different site" in plan.verdict.errors[0].message

    def test_site_and_parent_are_never_actions(self, importer, netbox):
        seed_prerequisites(netbox)
        plan = validate(importer, netbox, [row()])
        assert [action.endpoint for action in plan.create_actions] == [LOCATIONS, RACKS]


class TestCreateAndReuse:
    def test_new_child_and_rack_payloads_carry_relational_ids(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable()
        planned = plan.rows[0]
        child, rack = planned.actions
        assert child.payload == {
            "name": "EXA01-MMR1",
            "slug": "exa01_mmr1",
            "site": site["id"],
            "parent": parent["id"],
        }
        assert rack.payload["site"] == site["id"]
        assert isinstance(rack.payload["location"], Ref)
        assert rack.payload["u_height"] == 45
        assert rack.payload["width"] == 19
        assert rack.payload["status"] == "active"

    def test_new_rack_can_carry_an_existing_rack_role_slug(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        role = netbox.add(
            RACK_ROLES, name="Corporate Networks Rack", slug="corporate-networks-rack"
        )
        plan = validate(importer, netbox, [row(rack_role="corporate-networks-rack")])
        assert plan.verdict.is_applyable()
        rack = plan.rows[0].actions[-1]
        assert rack.payload["role"] == role["id"]
        assert plan.rows[0].resolved["rack_role"] == "Corporate Networks Rack (existing)"

    def test_rack_role_must_be_the_slug_not_the_display_name(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        netbox.add(RACK_ROLES, name="Compute Rack", slug="compute")
        plan = validate(importer, netbox, [row(rack_role="Compute Rack")])
        assert plan.verdict.is_applyable() is False
        error = next(issue for issue in plan.verdict.errors if issue.column == "rack_role")
        assert "slug" in error.message

    def test_existing_child_plans_only_the_rack(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        seed_child(netbox, site, parent)
        plan = validate(importer, netbox, [row()])
        assert [action.endpoint for action in plan.rows[0].actions] == [RACKS]

    def test_existing_matching_rack_is_an_idempotent_skip(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        child = seed_child(netbox, site, parent)
        seed_rack(netbox, site, child)
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable()
        assert plan.rows[0].outcome is RowOutcome.SKIP_EXISTS
        assert any(issue.code is Code.ALREADY_EXISTS for issue in plan.verdict.issues)

    def test_existing_rack_missing_role_is_updated_from_sheet(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        child = seed_child(netbox, site, parent)
        rack = seed_rack(netbox, site, child)
        role = netbox.add(RACK_ROLES, name="Compute", slug="compute")
        plan = validate(importer, netbox, [row(rack_role="compute")])
        assert plan.verdict.is_applyable()
        action = plan.rows[0].actions[-1]
        assert action.method == "patch"
        assert action.payload == {"id": rack["id"], "role": role["id"]}

        report = importer.apply(plan, Lookups(client=netbox))
        assert report.changed_count == 1
        assert netbox.patches[0].payloads == [{"id": rack["id"], "role": role["id"]}]
        assert netbox.stored(RACKS)[0]["role"]["slug"] == "compute"

    def test_existing_rack_role_is_overridden_from_sheet(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        child = seed_child(netbox, site, parent)
        old_role = netbox.add(RACK_ROLES, name="Storage", slug="storage")
        new_role = netbox.add(RACK_ROLES, name="Compute", slug="compute")
        seed_rack(netbox, site, child, role={"id": old_role["id"], "name": "Storage"})
        plan = validate(importer, netbox, [row(rack_role="compute")])
        assert plan.verdict.is_applyable()
        assert plan.rows[0].actions[-1].payload["role"] == new_role["id"]

    def test_existing_rack_with_matching_role_is_skipped(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        child = seed_child(netbox, site, parent)
        role = netbox.add(RACK_ROLES, name="Compute", slug="compute")
        seed_rack(netbox, site, child, role={"id": role["id"], "name": "Compute"})
        plan = validate(importer, netbox, [row(rack_role="compute")])
        assert plan.verdict.is_applyable()
        assert plan.rows[0].outcome is RowOutcome.SKIP_EXISTS
        assert plan.rows[0].actions == []

    def test_apply_creates_child_before_rack(self, importer, netbox):
        seed_prerequisites(netbox)
        plan = validate(importer, netbox, [row()])
        report = importer.apply(plan, Lookups(client=netbox))
        assert [post.endpoint for post in netbox.posts] == [LOCATIONS, RACKS]
        assert report.created_count == 2
        created_child = netbox.stored(LOCATIONS)[-1]
        created_rack = netbox.stored(RACKS)[-1]
        assert created_rack["location"]["id"] == created_child["id"]

    def test_many_racks_share_one_new_child_action_at_apply(self, importer, netbox):
        seed_prerequisites(netbox)
        plan = validate(
            importer,
            netbox,
            [row(), row(rack="EXA01-MMR1-RK0002")],
        )
        report = importer.apply(plan, Lookups(client=netbox))
        assert len(netbox.posted(LOCATIONS)[0].payloads) == 1
        assert len(netbox.posted(RACKS)[0].payloads) == 2
        assert report.created_count == 3


class TestOpaqueNamesAndSlugs:
    @pytest.mark.parametrize("rack_name", ["R01", "rack_with_underscores", "A-B-C-D-E"])
    def test_rack_name_segments_have_no_semantics(self, importer, netbox, rack_name):
        seed_prerequisites(netbox)
        plan = validate(importer, netbox, [row(rack=rack_name)])
        assert plan.verdict.is_applyable()
        assert plan.rows[0].actions[-1].payload["name"] == rack_name

    @pytest.mark.parametrize("slug", ["exa01-mmr1", "exa01_mmr1"])
    def test_hyphen_and_underscore_are_valid_child_slugs(self, importer, netbox, slug):
        seed_prerequisites(netbox)
        plan = validate(importer, netbox, [row(datahall_slug=slug)])
        assert plan.verdict.is_applyable()

    def test_invalid_slug_blocks_in_s2(self, importer, netbox):
        seed_prerequisites(netbox)
        plan = validate(importer, netbox, [row(datahall_slug="not a slug")])
        assert plan.verdict.stages_completed == ("S1", "S2")
        assert plan.verdict.errors[0].code is Code.SLUG_INVALID

    def test_height_above_netbox_limit_blocks_in_s2(self, importer, netbox):
        seed_prerequisites(netbox)
        plan = validate(importer, netbox, [row(rack_u_height="101")])
        assert plan.verdict.stages_completed == ("S1", "S2")
        assert plan.verdict.errors[0].expected == "a whole number from 1 to 100"

    def test_duplicate_rack_names_are_case_insensitive(self, importer, netbox):
        seed_prerequisites(netbox)
        plan = validate(
            importer,
            netbox,
            [row(rack="Mixed-Rack"), row(rack="mixed-rack")],
        )
        assert plan.verdict.stages_completed == ("S1", "S2", "S3")
        assert plan.verdict.errors[0].code is Code.DUPLICATE_RACK_IN_FILE


class TestRelationshipConflicts:
    def test_one_child_name_cannot_have_two_slugs_in_a_file(self, importer, netbox):
        seed_prerequisites(netbox)
        plan = validate(
            importer,
            netbox,
            [row(), row(datahall_slug="different", rack="R02")],
        )
        assert Code.SLUG_INCONSISTENT_IN_FILE in {issue.code for issue in plan.verdict.errors}

    def test_existing_child_under_another_parent_is_not_moved(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        other_parent = netbox.add(
            LOCATIONS,
            name="EXA02",
            slug="exa02",
            site={"id": site["id"], "name": site["name"]},
            parent=None,
        )
        seed_child(netbox, site, other_parent)
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable() is False
        assert "different parent" in plan.verdict.errors[0].message
        assert parent["id"] != other_parent["id"]

    def test_name_and_slug_resolving_to_different_children_blocks(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        seed_child(netbox, site, parent, slug="another-slug")
        seed_child(netbox, site, parent, name="Another Hall", slug="exa01_mmr1")
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable() is False
        assert "different child locations" in plan.verdict.errors[0].message

    def test_existing_rack_in_another_child_is_not_moved(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        wanted = seed_child(netbox, site, parent)
        other = seed_child(netbox, site, parent, name="Other Hall", slug="other-hall")
        seed_rack(netbox, site, other)
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable() is False
        assert "never moved" in plan.verdict.errors[0].message
        assert wanted["id"] != other["id"]

    def test_ambiguous_rack_name_lists_each_site_and_location(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        child = seed_child(netbox, site, parent)
        seed_rack(netbox, site, child)
        other_site = netbox.add(SITES, name="Other Site", slug="other")
        netbox.add(
            RACKS,
            name="exa01-mmr1-rk0001",
            site={"id": other_site["id"], "name": "Other Site"},
            location={"id": 999, "name": "Other Hall"},
            u_height=45,
        )
        plan = validate(importer, netbox, [row()])
        error = next(issue for issue in plan.verdict.errors if issue.column == "rack")
        assert "Example Site/EXA01-MMR1" in error.message
        assert "Other Site/Other Hall" in error.message

    def test_existing_rack_height_difference_blocks(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        child = seed_child(netbox, site, parent)
        seed_rack(netbox, site, child, u_height=42)
        plan = validate(importer, netbox, [row()])
        assert any(
            issue.code is Code.EXISTS_BUT_DIFFERS and issue.column == "rack_u_height"
            for issue in plan.verdict.errors
        )


class TestOptionalReferencesAndGate:
    def test_existing_tenant_is_attached_to_new_rack(self, importer, netbox):
        seed_prerequisites(netbox)
        tenant = netbox.add(TENANTS, name="Example Tenant", slug="example-tenant")
        plan = validate(importer, netbox, [row(tenant="Example Tenant")])
        rack = plan.rows[0].actions[-1]
        assert rack.payload["tenant"] == tenant["id"]

    def test_missing_tenant_blocks(self, importer, netbox):
        seed_prerequisites(netbox)
        plan = validate(importer, netbox, [row(tenant="Missing Tenant")])
        assert any(issue.code is Code.TENANT_NOT_FOUND for issue in plan.verdict.errors)

    def test_missing_rack_role_blocks(self, importer, netbox):
        seed_prerequisites(netbox)
        plan = validate(importer, netbox, [row(rack_role="missing-role")])
        assert any(issue.column == "rack_role" for issue in plan.verdict.errors)

    def test_validation_never_writes(self, importer, netbox):
        seed_prerequisites(netbox)
        validate(importer, netbox, [row()])
        assert netbox.posts == []

    def test_apply_refuses_a_blocked_plan(self, importer, netbox):
        plan = validate(importer, netbox, [row()])
        with pytest.raises(ApplyRefused):
            importer.apply(plan, Lookups(client=netbox))

    @override_settings(MAX_APPLY_ROWS=1)
    def test_row_limit_blocks(self, importer, netbox):
        seed_prerequisites(netbox)
        plan = validate(importer, netbox, [row(), row(rack="R02")])
        assert any(issue.code is Code.ROW_COUNT_OVER_LIMIT for issue in plan.verdict.errors)

    def test_descriptive_difference_is_a_warning(self, importer, netbox):
        site, parent = seed_prerequisites(netbox)
        child = seed_child(netbox, site, parent)
        seed_rack(netbox, site, child, description="Held")
        plan = validate(importer, netbox, [row(description="Requested")])
        assert plan.verdict.errors == []
        assert plan.verdict.needs_acknowledgement
        assert any(
            issue.severity is Severity.WARNING and issue.column == "description"
            for issue in plan.verdict.issues
        )


def test_old_hierarchy_contract_fails_structure_validation():
    with pytest.raises(SheetRejected) as rejected:
        legacy_sheet()
    codes = {issue.code for issue in rejected.value.issues}
    assert Code.REQUIRED_COLUMN_MISSING in codes
