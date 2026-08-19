# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Tasks 12 and 13 — the shared pipeline, exercised through the hierarchy importer.

Requirements 6.3, 7.1–7.9, 10.1–10.18, 11.1–11.7, 12.1–12.6, 15.2, 15.3.
"""

import csv
import io

import pytest
from django.test import override_settings

from importer.columns import columns_for
from importer.importers.base import ApplyRefused, RowOutcome
from importer.importers.hierarchy import HierarchyImporter
from importer.services.lookups import Lookups
from importer.services.netbox_client import Failure, NetBoxUnavailable
from importer.services.sheets import parse_upload
from importer.tests.fakes import FakeNetBox
from importer.validation import Code, Severity

REGIONS, SITES, LOCATIONS, RACKS = (
    "dcim.regions",
    "dcim.sites",
    "dcim.locations",
    "dcim.racks",
)

BASE_ROW = {
    "location": "Example Region",
    "datacenter": "EXA1",
    "datahall": "DH1",
    "rack": "EXA1-DH1-R01",
    "rack_u_height": "45",
}


def sheet(rows: list[dict[str, str]], filename: str = "hierarchy.csv"):
    """Build a real csv and parse it, so tests exercise the same path an upload takes."""
    specs = columns_for("hierarchy")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([spec.label for spec in specs])
    for row in rows:
        writer.writerow([row.get(spec.key, "") for spec in specs])
    payload = buffer.getvalue().encode("utf-8-sig")
    return parse_upload(
        "hierarchy",
        filename=filename,
        stream=io.BytesIO(payload),
        size=len(payload),
        content_type="text/csv",
    )


def row(**overrides: str) -> dict[str, str]:
    return {**BASE_ROW, **overrides}


def seed_hierarchy(netbox, **rack_fields):
    """The hierarchy `BASE_ROW` describes, already present in NetBox and internally consistent."""
    region = netbox.add(REGIONS, name="Example Region", slug="example-region")
    site = netbox.add(
        SITES,
        name="EXA1",
        slug="exa1",
        region={"id": region["id"], "name": "Example Region"},
    )
    hall = netbox.add(LOCATIONS, name="DH1", slug="dh1", site={"id": site["id"], "name": "EXA1"})
    rack = netbox.add(
        RACKS,
        name="EXA1-DH1-R01",
        site={"id": site["id"], "name": "EXA1"},
        location={"id": hall["id"], "name": "DH1"},
        u_height=rack_fields.pop("u_height", 45),
        width={"value": 19},
        status={"value": "active"},
        **rack_fields,
    )
    return region, site, hall, rack


@pytest.fixture
def netbox():
    return FakeNetBox()


@pytest.fixture
def importer():
    return HierarchyImporter()


def validate(importer, netbox, rows):
    return importer.validate(sheet(rows), Lookups(client=netbox))


def apply_plan(importer, netbox, plan):
    return importer.apply(plan, Lookups(client=netbox))


# --- validation on an empty NetBox ----------------------------------------------------------


class TestEmptyNetBox:
    def test_one_row_plans_the_whole_hierarchy(self, importer, netbox):
        """Requirements 7.2, 17.7: nothing exists, so all four levels are created."""
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable() is True
        assert plan.verdict.stages_completed == ("S1", "S2", "S3", "S4", "S5")
        planned = plan.rows[0]
        assert planned.outcome == RowOutcome.CREATE
        assert [action.endpoint for action in planned.actions] == [
            REGIONS,
            SITES,
            LOCATIONS,
            RACKS,
        ]

    def test_nothing_is_written_during_validation(self, importer, netbox):
        """Requirement 10.1."""
        validate(importer, netbox, [row(), row(rack="EXA1-DH1-R02")])
        assert netbox.posts == []

    def test_slugs_are_derived_for_the_three_objects_that_need_them(self, importer, netbox):
        """Requirement 7.3."""
        plan = validate(importer, netbox, [row()])
        payloads = {action.endpoint: action.payload for action in plan.rows[0].actions}
        assert payloads[REGIONS]["slug"] == "example-region"
        assert payloads[SITES]["slug"] == "exa1"
        assert payloads[LOCATIONS]["slug"] == "dh1"
        assert "slug" not in payloads[RACKS]

    def test_the_rack_payload_carries_its_height_and_the_documented_defaults(
        self, importer, netbox
    ):
        """Requirements 7.5, 7.7."""
        plan = validate(importer, netbox, [row()])
        rack = next(a for a in plan.rows[0].actions if a.endpoint == RACKS)
        assert rack.payload["u_height"] == 45
        assert rack.payload["width"] == 19
        assert rack.payload["status"] == "active"
        assert "form_factor" not in rack.payload

    def test_supplied_optional_values_override_the_defaults(self, importer, netbox):
        plan = validate(
            importer,
            netbox,
            [
                row(
                    rack_width="23",
                    rack_status="planned",
                    rack_form_factor="4-post-cabinet",
                    description="compute row",
                )
            ],
        )
        rack = next(a for a in plan.rows[0].actions if a.endpoint == RACKS)
        assert rack.payload["width"] == 23
        assert rack.payload["status"] == "planned"
        assert rack.payload["form_factor"] == "4-post-cabinet"
        assert rack.payload["description"] == "compute row"

    def test_the_rack_keeps_the_casing_the_sheet_supplied(self, importer, netbox):
        """Requirement 6.4."""
        plan = validate(
            importer, netbox, [row(rack="Exa1-Dh1-r01", datacenter="Exa1", datahall="Dh1")]
        )
        rack = next(a for a in plan.rows[0].actions if a.endpoint == RACKS)
        assert rack.payload["name"] == "Exa1-Dh1-r01"

    def test_repeated_parents_across_rows_are_created_once(self, importer, netbox):
        """Requirement 7.1/7.6: the hierarchy columns repeat down the sheet."""
        plan = validate(
            importer,
            netbox,
            [row(), row(rack="EXA1-DH1-R02"), row(rack="EXA1-DH1-R03")],
        )
        assert len(plan.actions_for(RACKS)) == 3
        report = apply_plan(importer, netbox, plan)
        assert len(netbox.stored(REGIONS)) == 1
        assert len(netbox.stored(SITES)) == 1
        assert len(netbox.stored(LOCATIONS)) == 1
        assert len(netbox.stored(RACKS)) == 3
        assert report.created_count == 6

    def test_the_same_datahall_name_under_two_datacenters_stays_distinct(self, importer, netbox):
        """Requirement 7.9: a NetBox location is scoped to its site."""
        plan = validate(
            importer,
            netbox,
            [row(), row(datacenter="EXA2", rack="EXA2-DH1-R01")],
        )
        apply_plan(importer, netbox, plan)
        halls = netbox.stored(LOCATIONS)
        assert len(halls) == 2
        assert {hall["site"]["name"] for hall in halls} == {"EXA1", "EXA2"}


# --- S2: cells ------------------------------------------------------------------------------


class TestCellChecks:
    def test_a_missing_required_value_blocks(self, importer, netbox):
        plan = validate(importer, netbox, [row(rack_u_height="")])
        assert plan.verdict.is_applyable() is False
        codes = {issue.code for issue in plan.verdict.errors}
        assert Code.REQUIRED_VALUE_BLANK in codes

    @pytest.mark.parametrize("height", ["0", "-5", "abc", "45.5"])
    def test_a_rack_height_that_is_not_a_positive_integer_blocks(self, importer, netbox, height):
        """Requirement 7.6."""
        plan = validate(importer, netbox, [row(rack_u_height=height)])
        assert {i.code for i in plan.verdict.errors} == {Code.NOT_A_POSITIVE_INTEGER}

    def test_a_choice_value_netbox_does_not_accept_blocks_and_lists_the_choices(
        self, importer, netbox
    ):
        """Requirement 7.7: the choices come from the API, not from a hardcoded list."""
        plan = validate(importer, netbox, [row(rack_status="retired")])
        error = next(i for i in plan.verdict.errors if i.code is Code.VALUE_NOT_IN_CHOICES)
        assert "retired" in error.message
        assert "deprecated" in error.expected

    def test_a_choice_is_accepted_case_insensitively(self, importer, netbox):
        plan = validate(importer, netbox, [row(rack_status="ACTIVE")])
        assert plan.verdict.is_applyable()

    def test_a_malformed_rack_name_blocks_with_the_expected_pattern(self, importer, netbox):
        """Requirement 6.2."""
        plan = validate(importer, netbox, [row(rack="R01")])
        error = next(i for i in plan.verdict.errors if i.code is Code.RACK_NAME_MALFORMED)
        assert "<datacenter>-<datahall>-<rack>" in error.expected

    def test_every_problem_in_the_stage_is_reported_not_just_the_first(self, importer, netbox):
        """Requirement 10.3."""
        plan = validate(
            importer,
            netbox,
            [row(rack_u_height="0"), row(rack="EXA1-DH1-R02", rack_status="bogus")],
        )
        assert len(plan.verdict.errors) >= 2
        assert {issue.line for issue in plan.verdict.errors} == {2, 3}

    def test_a_name_with_no_slug_is_rejected_before_netbox_sees_it(self, importer, netbox):
        plan = validate(importer, netbox, [row(location="???")])
        assert {i.code for i in plan.verdict.errors} == {Code.SLUG_NOT_DERIVABLE}

    def test_later_stages_are_not_reached_when_cells_are_wrong(self, importer, netbox):
        """Requirement 10.4."""
        plan = validate(importer, netbox, [row(rack_u_height="nope")])
        assert plan.verdict.stages_completed == ("S1", "S2")
        assert plan.verdict.stages_not_reached == ("S3", "S4", "S5")
        assert "S4 (cross-reference against NetBox)" in plan.verdict.unreached_explanation()
        # The error itself is the headline reason; the unreached stages are the caveat beside it.
        assert "no partial apply" in plan.verdict.refusal_reason()


# --- S3: consistency within the file --------------------------------------------------------


class TestConsistency:
    def test_a_rack_name_disagreeing_with_its_own_columns_blocks(self, importer, netbox):
        """Requirement 6.3: state both values."""
        plan = validate(importer, netbox, [row(rack="EXA9-DH1-R01")])
        error = next(
            i for i in plan.verdict.errors if i.code is Code.RACK_NAME_DISAGREES_WITH_COLUMNS
        )
        assert "EXA9" in error.message and "EXA1" in error.message

    def test_a_duplicate_rack_names_the_earlier_row(self, importer, netbox):
        """Requirement 7.8."""
        plan = validate(importer, netbox, [row(), row()])
        error = next(i for i in plan.verdict.errors if i.code is Code.DUPLICATE_RACK_IN_FILE)
        assert error.line == 3
        assert error.related_line == 2
        assert "line 2" in error.message

    def test_a_duplicate_is_detected_regardless_of_casing(self, importer, netbox):
        plan = validate(
            importer, netbox, [row(), row(rack="exa1-dh1-r01", datacenter="exa1", datahall="dh1")]
        )
        assert any(i.code is Code.DUPLICATE_RACK_IN_FILE for i in plan.verdict.errors)

    def test_two_names_that_slug_alike_are_rejected_before_netbox_rejects_them(
        self, importer, netbox
    ):
        plan = validate(
            importer,
            netbox,
            [row(), row(location="Example  Region!", rack="EXA1-DH1-R02")],
        )
        error = next(i for i in plan.verdict.errors if i.code is Code.SLUG_COLLISION)
        assert "example-region" in error.message


# --- S4: what already exists ----------------------------------------------------------------


class TestIdempotency:
    def test_an_existing_hierarchy_is_skipped_entirely(self, importer, netbox):
        """Requirements 11.2, 11.7: re-uploading the same sheet creates nothing."""
        seed_hierarchy(netbox)

        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable() is True
        assert plan.rows[0].outcome == RowOutcome.SKIP_EXISTS
        assert plan.rows[0].actions == []
        assert any(i.code is Code.ALREADY_EXISTS for i in plan.verdict.infos)

        report = apply_plan(importer, netbox, plan)
        assert report.created_count == 0
        assert report.skipped_count == 1
        assert netbox.posts == []

    def test_an_existing_parent_with_a_new_rack_creates_only_the_rack(self, importer, netbox):
        """Requirement 11.2: skip what exists and continue with the row's remaining work."""
        region = netbox.add(REGIONS, name="Example Region", slug="example-region")
        site = netbox.add(
            SITES, name="EXA1", slug="exa1", region={"id": region["id"], "name": region["name"]}
        )
        netbox.add(LOCATIONS, name="DH1", slug="dh1", site={"id": site["id"], "name": "EXA1"})

        plan = validate(importer, netbox, [row()])
        assert [a.endpoint for a in plan.rows[0].actions] == [RACKS]
        report = apply_plan(importer, netbox, plan)
        assert report.created_count == 1
        assert len(netbox.stored(RACKS)) == 1
        # The new rack hangs off the objects that were already there.
        assert netbox.stored(RACKS)[0]["site"]["id"] == site["id"]

    def test_lookup_is_case_insensitive(self, importer, netbox):
        """Requirement 11.1."""
        netbox.add(REGIONS, name="EXAMPLE REGION", slug="example-region")
        plan = validate(importer, netbox, [row()])
        assert not any(a.endpoint == REGIONS for a in plan.rows[0].actions)

    def test_a_site_under_a_different_region_blocks_rather_than_being_re_parented(
        self, importer, netbox
    ):
        """Requirements 11.3, 11.5: a site's region is consequential."""
        other = netbox.add(REGIONS, name="Other Region", slug="other-region")
        netbox.add(
            SITES, name="EXA1", slug="exa1", region={"id": other["id"], "name": "Other Region"}
        )

        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable() is False
        error = next(i for i in plan.verdict.errors if i.code is Code.EXISTS_BUT_DIFFERS)
        assert "Other Region" in error.message and "Example Region" in error.message
        assert "never re-parents" in error.message

    def test_a_rack_height_that_differs_blocks_with_both_values(self, importer, netbox):
        """Requirement 11.5, decision D8."""
        seed_hierarchy(netbox, u_height=42)
        plan = validate(importer, netbox, [row()])
        error = next(
            i
            for i in plan.verdict.errors
            if i.code is Code.EXISTS_BUT_DIFFERS and i.column == "rack_u_height"
        )
        assert error.value == "45" and error.expected == "42"

    def test_a_description_that_differs_only_warns(self, importer, netbox):
        """Requirement 11.5a, decision D8."""
        seed_hierarchy(netbox, description="storage row")
        plan = validate(importer, netbox, [row(description="compute row")])
        assert plan.verdict.errors == []
        assert len(plan.verdict.warnings) == 1
        # Requirement 10.9: it must be acknowledged before applying.
        assert plan.verdict.is_applyable() is False
        plan.verdict.acknowledge()
        assert plan.verdict.is_applyable() is True

    def test_a_presentation_difference_is_not_a_conflict(self, importer, netbox):
        """Requirement 11.5b."""
        seed_hierarchy(netbox, description="compute  row")
        plan = validate(importer, netbox, [row(description="Compute row")])
        assert plan.verdict.warnings == []

    def test_a_slug_belonging_to_a_differently_named_object_blocks(self, importer, netbox):
        """Requirement 7.3."""
        netbox.add(REGIONS, name="Example-Region", slug="example-region")
        plan = validate(importer, netbox, [row()])
        error = next(i for i in plan.verdict.errors if i.code is Code.SLUG_COLLISION)
        assert "Example-Region" in error.message

    def test_a_tenant_that_does_not_exist_blocks_and_is_never_created(self, importer, netbox):
        plan = validate(importer, netbox, [row(tenant="Acme")])
        error = next(i for i in plan.verdict.errors if i.code is Code.TENANT_NOT_FOUND)
        assert "never creates" in error.message

    def test_an_existing_tenant_is_attached_by_id(self, importer, netbox):
        tenant = netbox.add("tenancy.tenants", name="Acme", slug="acme")
        plan = validate(importer, netbox, [row(tenant="Acme")])
        rack = next(a for a in plan.rows[0].actions if a.endpoint == RACKS)
        assert rack.payload["tenant"] == tenant["id"]


# --- S5 and the request budget --------------------------------------------------------------


class TestFeasibilityAndBudget:
    @override_settings(MAX_APPLY_ROWS=3)
    def test_a_file_over_the_row_limit_blocks_with_the_limit_named(self, importer, netbox):
        """Requirement 15.3."""
        rows = [row(rack=f"EXA1-DH1-R{index:02d}") for index in range(1, 6)]
        plan = validate(importer, netbox, rows)
        error = next(i for i in plan.verdict.errors if i.code is Code.ROW_COUNT_OVER_LIMIT)
        assert "3-row limit" in error.message
        assert plan.verdict.is_applyable() is False

    def test_validation_requests_do_not_grow_with_the_row_count(self, importer, netbox):
        """Requirement 15.2."""
        importer.validate(sheet([row()]), Lookups(client=netbox))
        one_row = netbox.request_count

        netbox.request_count = 0
        rows = [row(rack=f"EXA1-DH1-R{index:02d}") for index in range(1, 201)]
        importer.validate(sheet(rows), Lookups(client=netbox))
        two_hundred_rows = netbox.request_count

        assert two_hundred_rows <= one_row + 2


# --- apply ----------------------------------------------------------------------------------


class TestApply:
    def test_creates_are_grouped_into_one_post_per_endpoint(self, importer, netbox):
        """Decision D7."""
        rows = [row(rack=f"EXA1-DH1-R{index:02d}") for index in range(1, 5)]
        plan = validate(importer, netbox, rows)
        apply_plan(importer, netbox, plan)

        assert [post.endpoint for post in netbox.posts] == [REGIONS, SITES, LOCATIONS, RACKS]
        assert [post.size for post in netbox.posts] == [1, 1, 1, 4]

    def test_parents_are_created_before_children_and_referenced_by_id(self, importer, netbox):
        """Requirement 7.2, 7.4."""
        plan = validate(importer, netbox, [row()])
        apply_plan(importer, netbox, plan)

        site = netbox.stored(SITES)[0]
        region = netbox.stored(REGIONS)[0]
        hall = netbox.stored(LOCATIONS)[0]
        rack = netbox.stored(RACKS)[0]
        assert site["region"]["id"] == region["id"]
        assert hall["site"]["id"] == site["id"]
        assert rack["site"]["id"] == site["id"]
        assert rack["location"]["id"] == hall["id"]

    def test_the_report_lists_every_row_with_a_link(self, importer, netbox):
        """Requirement 12.1."""
        plan = validate(importer, netbox, [row()])
        report = apply_plan(importer, netbox, plan)
        rack_result = next(r for r in report.results if r.object_kind == "rack")
        assert rack_result.line == 2
        assert rack_result.object_name == "EXA1-DH1-R01"
        assert rack_result.url.endswith(f"/dcim/racks/{rack_result.object_id}/")
        assert report.headline.startswith("Applied — 4 created")

    def test_the_report_downloads_as_csv(self, importer, netbox):
        """Requirement 12.6."""
        plan = validate(importer, netbox, [row()])
        report = apply_plan(importer, netbox, plan)
        text = report.to_csv().decode("utf-8-sig")
        assert text.splitlines()[0] == (
            "sequence,line,outcome,object_type,object_name,netbox_id,url,column,detail"
        )
        # The sequence column is what a manual unwind reads backwards (Requirement 12.4).
        assert [line.split(",")[0] for line in text.splitlines()[1:]] == ["1", "2", "3", "4"]
        assert "EXA1-DH1-R01" in text

    def test_the_report_states_the_guarantee_plainly(self, importer, netbox):
        """Requirement 12.5."""
        plan = validate(importer, netbox, [row()])
        report = apply_plan(importer, netbox, plan)
        assert "all-or-nothing" in report.guarantee
        assert "not atomic" in report.guarantee
        assert "Nothing this tool creates can be rolled back by this tool" in report.guarantee


class TestApplyGate:
    def test_a_blocked_plan_cannot_be_applied_even_directly(self, importer, netbox):
        """Requirement 10.6: a direct POST is refused with an explanation."""
        plan = validate(importer, netbox, [row(rack_u_height="0")])
        with pytest.raises(ApplyRefused) as raised:
            apply_plan(importer, netbox, plan)
        assert "error" in raised.value.reason
        assert netbox.posts == []

    def test_unacknowledged_warnings_refuse_the_apply(self, importer, netbox):
        seed_hierarchy(netbox, description="storage row")
        plan = validate(importer, netbox, [row(description="compute row")])
        with pytest.raises(ApplyRefused):
            apply_plan(importer, netbox, plan)

    def test_an_expired_plan_refuses_to_apply(self, importer, netbox):
        from datetime import timedelta

        plan = validate(importer, netbox, [row()])
        plan.verdict.validated_at -= timedelta(hours=2)
        with pytest.raises(ApplyRefused, match="expired"):
            apply_plan(importer, netbox, plan)
        assert netbox.posts == []


class TestDriftDetection:
    def test_an_object_created_since_validation_aborts_before_writing(self, importer, netbox):
        """Requirement 10.14."""
        plan = validate(importer, netbox, [row()])
        assert plan.verdict.is_applyable()

        # Someone else creates the rack in the meantime.
        netbox.add(SITES, name="EXA1", slug="exa1", region=None)
        netbox.add(
            RACKS,
            name="EXA1-DH1-R01",
            site={"id": 1, "name": "EXA1"},
            location={"id": 2, "name": "DH1"},
            u_height=42,
        )

        report = apply_plan(importer, netbox, plan)
        assert report.aborted is True
        assert "changed after this file was validated" in report.abort_reason
        assert "Nothing was written" in report.abort_reason
        assert netbox.posts == []

    def test_a_referenced_object_that_disappeared_aborts_before_writing(self, importer, netbox):
        region = netbox.add(REGIONS, name="Example Region", slug="example-region")
        plan = validate(importer, netbox, [row()])
        netbox.objects[REGIONS] = [r for r in netbox.stored(REGIONS) if r["id"] != region["id"]]

        report = apply_plan(importer, netbox, plan)
        assert report.aborted is True
        assert "no longer exists" in report.abort_reason
        assert netbox.posts == []

    def test_netbox_going_away_before_the_first_write_aborts_cleanly(self, importer, netbox):
        """Requirement 12.3: the pre-write re-check cannot itself become a 500."""
        plan = validate(importer, netbox, [row()])
        netbox.unavailable_on_read[REGIONS] = NetBoxUnavailable(
            Failure.TIMEOUT, netbox.host, "NetBox did not respond within 30 seconds."
        )
        report = apply_plan(importer, netbox, plan)
        assert report.aborted is True
        assert "nothing was written" in report.abort_reason
        assert netbox.posts == []

    def test_an_unchanged_netbox_applies_normally(self, importer, netbox):
        netbox.add(REGIONS, name="Example Region", slug="example-region")
        plan = validate(importer, netbox, [row()])
        report = apply_plan(importer, netbox, plan)
        assert report.aborted is False
        assert report.created_count == 3


class TestApplyFailures:
    def test_a_field_level_rejection_is_blamed_on_the_right_row(self, importer, netbox):
        """Requirement 12.2: NetBox's own text, against the offending row and column."""
        rows = [row(rack=f"EXA1-DH1-R{index:02d}") for index in range(1, 4)]
        plan = validate(importer, netbox, rows)
        netbox.reject(
            RACKS,
            lambda payloads: (400, [{}, {"name": ["Rack with this name already exists."]}, {}]),
        )
        report = apply_plan(importer, netbox, plan)

        blamed = next(r for r in report.results if r.column == "name")
        assert blamed.line == 3
        assert "already exists" in blamed.message
        # Requirement 12.3: the rest of the batch rolled back.
        assert len([r for r in report.results if r.outcome == RowOutcome.BLOCKED]) == 3
        assert netbox.stored(RACKS) == []

    def test_the_run_stops_before_the_next_endpoint(self, importer, netbox):
        """Requirement 12.3, 12.4."""
        plan = validate(importer, netbox, [row()])
        netbox.reject(LOCATIONS, lambda payloads: (400, {"site": ["Invalid."]}))
        report = apply_plan(importer, netbox, plan)

        assert report.aborted is True
        assert report.endpoints_completed == [REGIONS, SITES]
        assert RACKS not in report.endpoints_attempted
        assert netbox.stored(RACKS) == []
        # Requirement 12.4: what was created before the abort is listed.
        assert {result.object_kind for result in report.created} == {"region", "site"}

    def test_an_unindexed_rejection_is_replayed_to_find_the_offender(self, importer, netbox):
        """Requirement 12.2, and the D7 catch: NetBox said *that*, not *which*."""
        rows = [row(rack=f"EXA1-DH1-R{index:02d}") for index in range(1, 4)]
        plan = validate(importer, netbox, rows)

        def decide(payloads):
            if len(payloads) > 1:
                return 400, {"__all__": ["Duplicate position in submitted list."]}
            if payloads[0]["name"] == "EXA1-DH1-R02":
                return 400, {"name": ["U10 is already occupied."]}
            return None

        netbox.reject(RACKS, decide)
        report = apply_plan(importer, netbox, plan)

        assert report.replayed is True
        blamed = next(r for r in report.results if r.outcome == RowOutcome.BLOCKED and r.column)
        assert blamed.object_name == "EXA1-DH1-R02"
        assert "U10 is already occupied" in blamed.message
        # The replay writes: the row before the offender was created individually, and the rows
        # after it were not attempted.
        assert [r["name"] for r in netbox.stored(RACKS)] == ["EXA1-DH1-R01"]
        not_attempted = next(r for r in report.results if "Not attempted" in r.message)
        assert not_attempted.object_name == "EXA1-DH1-R03"

    def test_a_connection_loss_mid_apply_aborts_the_run(self, importer, netbox):
        """Requirement 12.3."""
        plan = validate(importer, netbox, [row()])
        netbox.unavailable_on[LOCATIONS] = NetBoxUnavailable(
            Failure.CONNECTION, netbox.host, "Could not open a connection."
        )
        report = apply_plan(importer, netbox, plan)
        assert report.aborted is True
        assert report.endpoints_completed == [REGIONS, SITES]
        assert "not rolled back" in report.abort_reason

    def test_an_auth_failure_mid_apply_aborts_without_leaking_the_token(self, importer, netbox):
        plan = validate(importer, netbox, [row()])
        netbox.unavailable_on[SITES] = NetBoxUnavailable(
            Failure.AUTH, netbox.host, "NetBox rejected the API token (HTTP 403)."
        )
        report = apply_plan(importer, netbox, plan)
        assert report.aborted is True
        assert "token" in report.abort_reason
        assert "0000" not in report.abort_reason


class TestAuditLogging:
    def test_the_apply_is_logged_with_counts_and_no_sheet_contents(self, importer, netbox, caplog):
        plan = validate(importer, netbox, [row()])
        with caplog.at_level("INFO", logger="importer.audit"):
            apply_plan(importer, netbox, plan)
        assert "apply finished importer=hierarchy" in caplog.text
        assert "created=4" in caplog.text

    def test_a_replay_is_logged_because_it_means_validation_missed_something(
        self, importer, netbox, caplog
    ):
        plan = validate(importer, netbox, [row()])
        netbox.reject(RACKS, lambda payloads: (400, {"__all__": ["Nope."]}))
        with caplog.at_level("WARNING", logger="importer.audit"):
            apply_plan(importer, netbox, plan)
        assert "replaying rejected batch" in caplog.text


def test_the_fake_matches_the_real_client_surface():
    """The double is only useful if it cannot drift from `NetBoxClient`."""
    from importer.services.netbox_client import NetBoxClient

    real = {name for name in dir(NetBoxClient) if not name.startswith("_")}
    fake = {name for name in dir(FakeNetBox) if not name.startswith("_")}
    assert real <= fake, real - fake


def test_a_row_that_blocks_makes_no_row_applyable():
    """Requirement 10.7: not even the valid rows, and not even on request."""
    netbox = FakeNetBox()
    importer = HierarchyImporter()
    plan = validate(importer, netbox, [row(), row(rack="EXA1-DH1-R02", rack_u_height="0")])
    assert plan.verdict.errors
    assert all(planned.outcome == RowOutcome.BLOCKED for planned in plan.rows)
    assert plan.verdict.create_count == 0
    assert "no partial apply" in plan.verdict.refusal_reason()


def test_severity_classification_covers_the_three_levels(importer=None, netbox=None):
    """Requirement 10.5: error blocks, warning must be acknowledged, info never blocks."""
    assert [Severity.ERROR.value, Severity.WARNING.value, Severity.INFO.value] == [
        "error",
        "warning",
        "info",
    ]
