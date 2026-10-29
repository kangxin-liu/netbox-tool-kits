# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 11 — the validation gate and the conflict comparison.

Requirements 10.4–10.10, 10.15, 10.16, 11.5, 11.5a, 11.5b, 11.5c, and decision D8.
"""

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from importer.columns import ColumnSpec, Conflict, columns_for, spec_by_key
from importer.validation import (
    STAGES,
    Code,
    Severity,
    ValidationIssue,
    Verdict,
    collapse,
    compare_to_existing,
    netbox_text,
    values_agree,
)


def issue(severity: Severity, code: Code = Code.ALREADY_EXISTS, line: int = 4) -> ValidationIssue:
    return ValidationIssue(
        severity=severity, code=code, file="sheet.csv", sheet="Hierarchy", line=line
    )


def verdict(*issues: ValidationIssue, **kwargs) -> Verdict:
    kwargs.setdefault("stages_completed", STAGES)
    return Verdict(
        file="sheet.csv", sheet="Hierarchy", fingerprint="abc123", issues=list(issues), **kwargs
    )


class TestGate:
    def test_a_clean_verdict_is_applyable(self):
        assert verdict().is_applyable() is True
        assert verdict().refusal_reason() is None

    def test_a_single_error_blocks_the_whole_file(self):
        """Requirements 10.6, 10.7: no partial apply, even of the valid rows."""
        blocked = verdict(issue(Severity.ERROR), issue(Severity.INFO))
        assert blocked.is_applyable() is False
        reason = blocked.refusal_reason()
        assert "1 error" in reason
        assert "no partial apply" in reason

    def test_info_issues_never_block(self):
        """Requirement 10.5: already-exists skips are INFO."""
        assert verdict(issue(Severity.INFO), issue(Severity.INFO)).is_applyable() is True

    def test_warnings_must_be_acknowledged_before_applying(self):
        """Requirement 10.9."""
        warned = verdict(issue(Severity.WARNING))
        assert warned.needs_acknowledgement is True
        assert warned.is_applyable() is False
        assert "acknowledged" in warned.refusal_reason()

        warned.acknowledge()
        assert warned.needs_acknowledgement is False
        assert warned.is_applyable() is True
        assert warned.acknowledged_at is not None

    def test_an_acknowledgement_cannot_rescue_an_error(self):
        blocked = verdict(issue(Severity.ERROR), issue(Severity.WARNING))
        blocked.acknowledge()
        assert blocked.is_applyable() is False

    def test_an_already_applied_plan_is_refused_and_points_at_its_report(self):
        """Requirement 10.16."""
        applied = verdict(applied_at=timezone.now())
        assert applied.is_applyable() is False
        assert "already been applied" in applied.refusal_reason()

    def test_an_already_applied_plan_outranks_every_other_reason(self):
        applied = verdict(issue(Severity.ERROR), applied_at=timezone.now())
        assert "already been applied" in applied.refusal_reason()


class TestUnreachedStages:
    def test_an_incomplete_run_names_the_stages_not_reached(self):
        """Requirement 10.4."""
        partial = verdict(stages_completed=("S1",))
        assert partial.stages_not_reached == ("S2", "S3", "S4", "S5")
        assert partial.complete is False
        explanation = partial.unreached_explanation()
        assert "S4 (cross-reference against NetBox)" in explanation

    def test_an_incomplete_run_is_not_applyable_even_with_no_errors(self):
        """Stopping early means unknown, not clean."""
        partial = verdict(stages_completed=("S1", "S2"))
        assert partial.is_applyable() is False
        assert "not reached" in partial.refusal_reason()

    def test_a_complete_run_has_nothing_unreached(self):
        assert verdict().complete is True
        assert verdict().unreached_explanation() == ""


class TestExpiry:
    @override_settings(PLAN_EXPIRY_MINUTES=30)
    def test_a_plan_expires_after_the_configured_interval(self):
        """Requirement 10.15."""
        now = timezone.now()
        plan = verdict(validated_at=now)
        assert plan.expires_at == now + timedelta(minutes=30)
        assert plan.is_expired(now + timedelta(minutes=29)) is False
        assert plan.is_expired(now + timedelta(minutes=31)) is True

    @override_settings(PLAN_EXPIRY_MINUTES=5)
    def test_the_interval_is_configurable(self):
        now = timezone.now()
        assert verdict(validated_at=now).is_expired(now + timedelta(minutes=6)) is True

    @override_settings(PLAN_EXPIRY_MINUTES=30)
    def test_an_expired_plan_is_refused_with_a_reason_that_says_why(self):
        now = timezone.now()
        plan = verdict(validated_at=now - timedelta(hours=2))
        assert plan.is_applyable(now) is False
        reason = plan.refusal_reason(now)
        assert "expired" in reason and "NetBox may have changed" in reason


class TestHeadline:
    def test_a_ready_plan_reports_its_counts(self):
        """Requirement 10.10.

        Rows and objects are both named because they differ: one hierarchy row can create four
        objects, and it is the objects the operator is consenting to.
        """
        ready = verdict(row_count=10, create_count=7, object_count=19, skip_count=3)
        assert ready.headline() == ("Ready — 7 row(s) creating 19 object(s), 3 already present")

    def test_a_blocked_plan_reports_the_error_count(self):
        blocked = verdict(issue(Severity.ERROR), issue(Severity.ERROR))
        assert "Blocked — 2 error(s)" in blocked.headline()
        assert "Nothing will be written" in blocked.headline()

    def test_acknowledged_warnings_are_counted_in_the_headline(self):
        warned = verdict(issue(Severity.WARNING), create_count=4, skip_count=0)
        assert "0 of 1 warning(s) acknowledged" in warned.headline()
        warned.acknowledge()
        assert "1 of 1 warning(s) acknowledged" in warned.headline()


# --- comparison against an existing object --------------------------------------------------


HIERARCHY = spec_by_key("hierarchy")
DEVICES = spec_by_key("devices")


class TestNetBoxValueReading:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, ""),
            ("EXA1", "EXA1"),
            ("  EXA1  ", "EXA1"),
            (45, "45"),
            (45.0, "45"),
            ({"value": "active", "label": "Active"}, "active"),
            ({"value": 19, "label": "19 inches"}, "19"),
            ({"id": 3, "name": "Acme", "slug": "acme"}, "Acme"),
            ({"id": 3, "model": "Example-Switch-32C"}, "Example-Switch-32C"),
            ({"id": 7}, "7"),
            (True, "true"),
        ],
    )
    def test_netbox_shapes_are_reduced_to_comparable_text(self, raw, expected):
        assert netbox_text(raw) == expected

    def test_free_text_collapses_whitespace_and_case(self):
        assert collapse("  Compute   Row\n") == "compute row"


class TestValuesAgree:
    def test_a_number_written_as_text_matches(self):
        """Requirement 11.5b: 45 and 45.0 are the same rack height."""
        spec = HIERARCHY["rack_u_height"]
        assert values_agree(spec, "45", 45)
        assert values_agree(spec, "45", 45.0)
        assert not values_agree(spec, "45", 42)

    def test_a_choice_matches_its_value_not_its_label(self):
        spec = HIERARCHY["rack_status"]
        assert values_agree(spec, "active", {"value": "active", "label": "Active"})
        assert not values_agree(spec, "planned", {"value": "active", "label": "Active"})

    def test_free_text_ignores_presentation(self):
        """Requirement 11.5b: a difference of presentation alone is not a conflict."""
        spec = HIERARCHY["description"]
        assert values_agree(spec, "  Compute   row ", "compute row")
        assert not values_agree(spec, "compute row", "storage row")


class TestConflictClassification:
    def test_a_blank_cell_supplies_no_opinion(self):
        assert compare_to_existing(HIERARCHY["description"], "", "whatever NetBox holds") is None
        assert compare_to_existing(HIERARCHY["rack_status"], "   ", "planned") is None

    def test_an_identity_column_cannot_conflict(self):
        """A different prerequisite slug denotes a different site, not a field update."""
        assert compare_to_existing(HIERARCHY["site_slug"], "exa1", "exa9") is None

    def test_matching_values_produce_nothing(self):
        assert compare_to_existing(HIERARCHY["rack_u_height"], "45", 45) is None

    def test_a_consequential_difference_blocks(self):
        """Requirement 11.5: a rack's u_height is consequential."""
        error = compare_to_existing(
            HIERARCHY["rack_u_height"], "45", 42, object_label="Rack EXA1-DH1-R01"
        )
        assert error is not None
        assert error.severity is Severity.ERROR
        assert error.code is Code.EXISTS_BUT_DIFFERS
        # Requirement 11.5: name the field, the sheet value, and the NetBox value.
        assert error.column == "rack_u_height"
        assert error.value == "45"
        assert error.expected == "42"
        assert "'45'" in error.message and "'42'" in error.message
        assert "Rack EXA1-DH1-R01" in error.message
        # And say why it cannot simply be updated (Requirement 11.3).
        assert "never updates" in error.message

    def test_a_descriptive_difference_warns_instead(self):
        """Requirement 11.5a: nothing downstream reads it, so it is acknowledgeable."""
        warning = compare_to_existing(HIERARCHY["description"], "compute row", "storage row")
        assert warning.severity is Severity.WARNING
        assert "left as it is" in warning.message

    @pytest.mark.parametrize(
        ("sheet", "key"),
        [
            ("hierarchy", "description"),
            ("hierarchy", "tenant"),
            ("hierarchy", "rack_status"),
            ("devices", "status"),
            ("devices", "serial"),
            ("devices", "asset_tag"),
            ("devices", "tenant"),
            ("cabling", "label"),
            ("cabling", "color"),
            ("cabling", "length"),
            ("cabling", "length_unit"),
        ],
    )
    def test_the_descriptive_fields_named_in_the_requirement_all_warn(self, sheet, key):
        """Requirement 11.5a lists exactly these as acknowledgeable rather than blocking."""
        spec = spec_by_key(sheet)[key]
        warning = compare_to_existing(spec, "2", "9" if spec.numeric else "netbox value")
        assert warning.severity is Severity.WARNING

    def test_rack_form_factor_is_descriptive(self):
        """Requirement 11.5a lists it by its NetBox 4.2+ name."""
        warning = compare_to_existing(
            HIERARCHY["rack_form_factor"], "4-post-cabinet", {"value": "2-post-frame"}
        )
        assert warning.severity is Severity.WARNING

    @pytest.mark.parametrize("key", ["rack", "rack_face", "bottom_ru", "ru_height", "model"])
    def test_the_consequential_device_fields_all_block(self, key):
        """Requirement 11.5: a device's rack, position, face, and device type."""
        error = compare_to_existing(DEVICES[key], "7", "9")
        assert error.severity is Severity.ERROR

    def test_an_empty_netbox_value_is_reported_readably(self):
        warning = compare_to_existing(HIERARCHY["description"], "compute row", None)
        assert warning.expected == "(empty)"
        assert "(empty)" in warning.message

    def test_the_issue_can_carry_a_deep_link_to_the_existing_object(self):
        """Requirement 16.7."""
        error = compare_to_existing(
            HIERARCHY["rack_u_height"],
            "45",
            42,
            object_label="Rack EXA1-DH1-R01",
            related_url="https://netbox.example.com/dcim/racks/9/",
        )
        assert error.related_url.endswith("/dcim/racks/9/")


def test_every_column_declares_which_side_of_the_conflict_table_it_is_on():
    """Requirement 11.5c: one table in code, and adding a column forces the decision."""
    for sheet in ("hierarchy", "devices", "cabling"):
        for spec in columns_for(sheet):
            assert isinstance(spec.conflict, Conflict)
    # And the field has no default, so a new column cannot quietly inherit one.
    with pytest.raises(TypeError):
        ColumnSpec(key="new", label="new", required=False, help_text="no conflict declared")
