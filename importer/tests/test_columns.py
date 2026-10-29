# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 5 — the sheet contract. Requirements 4.3, 4.6, 4.7, 11.5c, and the column lists in 7/8/9."""

import re

import pytest

from importer import columns
from importer.columns import Conflict

# Straight from the requirements tables. Kept literal here on purpose: if someone edits a column
# tuple, this list is what argues with them.
EXPECTED_REQUIRED = {
    "hierarchy": (
        "site_slug",
        "parent_location_slug",
        "datahall",
        "datahall_slug",
        "rack",
        "rack_u_height",
    ),
    "devices": ("rack", "rack_face", "bottom_ru", "ru_height", "device_name", "model"),
    # `*_end_kind` are optional: Requirement 9.1 gives them a default, and a required column's
    # blank cell is rejected before a default can apply.
    "cabling": ("a_end", "a_port", "b_end", "b_port", "cable_type"),
}

EXPECTED_OPTIONAL = {
    "hierarchy": (
        "rack_role",
        "rack_width",
        "rack_status",
        "rack_form_factor",
        "tenant",
        "description",
    ),
    "devices": (
        "serial",
        "manufacturer",
        "device_role",
        "status",
        "asset_tag",
        "tenant",
        "loopback_ip4",
        "loopback_ip6",
        "management_ip4",
        "management_ip6",
        "primary_address_v4",
        "primary_address_v6",
    ),
    # `cable_profile` and `cable_group` are the D10 pair, both optional: a sheet that uses neither
    # is every cabling sheet written before them (Requirements 9.13, 9.15). The two
    # `*_interface_type` columns are D12's, and are read only when a physical interface has to be
    # created and the device type does not define it (Requirement 9.26). `*_parent` are D13's
    # optional child-to-physical-parent mapping (Requirement 9.34).
    "cabling": (
        "a_end_kind",
        "b_end_kind",
        "a_port_side",
        "b_port_side",
        "a_parent",
        "b_parent",
        "a_interface_type",
        "b_interface_type",
        "cable_profile",
        "cable_group",
        "cable_status",
        "label",
        "length",
        "length_unit",
        "color",
    ),
}

# The D8 split from design.md. Everything not named here is IDENTITY.
EXPECTED_CONSEQUENTIAL = {
    "hierarchy": {"datahall_slug", "rack_u_height", "rack_width"},
    "devices": {
        "rack",
        "rack_face",
        "bottom_ru",
        "ru_height",
        "model",
        "loopback_ip4",
        "loopback_ip6",
        "management_ip4",
        "management_ip6",
        "primary_address_v4",
        "primary_address_v6",
    },
    # `cable_profile` joins `cable_type`: it is what NetBox traces lanes through, so an existing
    # cable profiled differently from the sheet is a wrong picture, not a wrong label (D10).
    "cabling": {"cable_type", "cable_profile"},
}

EXPECTED_DESCRIPTIVE = {
    "hierarchy": {"rack_role", "rack_status", "rack_form_factor", "tenant", "description"},
    "devices": {"serial", "manufacturer", "device_role", "status", "asset_tag", "tenant"},
    # The two `*_interface_type` columns are DESCRIPTIVE only because `Conflict` is required on
    # every spec and has no value for "not comparable": the column is read when the interface does
    # *not* exist, and where it does, Requirement 9.27's INFO is an explicit check rather than the
    # generic compare-to-existing path. Declarative, exactly as `cable_profile`'s class is (D12).
    "cabling": {
        "a_interface_type",
        "b_interface_type",
        "cable_status",
        "label",
        "length",
        "length_unit",
        "color",
    },
}

SHEETS = sorted(columns.SHEETS)


@pytest.mark.parametrize("importer", SHEETS)
def test_required_columns_match_the_requirements(importer):
    assert columns.required_keys(importer) == EXPECTED_REQUIRED[importer]


@pytest.mark.parametrize("importer", SHEETS)
def test_optional_columns_match_the_requirements(importer):
    optional = tuple(spec.key for spec in columns.columns_for(importer) if not spec.required)
    assert set(optional) == set(EXPECTED_OPTIONAL[importer])


@pytest.mark.parametrize("importer", SHEETS)
def test_the_d8_split_matches_the_design_table(importer):
    by_conflict: dict[Conflict, set[str]] = {}
    for spec in columns.columns_for(importer):
        by_conflict.setdefault(spec.conflict, set()).add(spec.key)
    assert by_conflict.get(Conflict.CONSEQUENTIAL, set()) == EXPECTED_CONSEQUENTIAL[importer]
    assert by_conflict.get(Conflict.DESCRIPTIVE, set()) == EXPECTED_DESCRIPTIVE[importer]


@pytest.mark.parametrize("importer", SHEETS)
def test_every_column_carries_a_conflict_classification(importer):
    """Requirement 11.5c: the field has no default, so this can only fail by deleting it."""
    for spec in columns.columns_for(importer):
        assert isinstance(spec.conflict, Conflict)


@pytest.mark.parametrize("importer", SHEETS)
def test_every_column_has_help_text_and_a_unique_key(importer):
    specs = columns.columns_for(importer)
    keys = [spec.key for spec in specs]
    assert len(keys) == len(set(keys))
    for spec in specs:
        assert spec.help_text.strip()
        assert spec.key == spec.key.lower()
        assert re.fullmatch(r"[a-z][a-z0-9_]*", spec.key)


@pytest.mark.parametrize("importer", SHEETS)
def test_guidance_states_required_ness_and_lists_choices(importer):
    for spec in columns.columns_for(importer):
        guidance = spec.guidance
        assert guidance.startswith("required" if spec.required else "optional")
        if spec.choices:
            assert all(choice in guidance for choice in spec.choices)


@pytest.mark.parametrize("importer", SHEETS)
def test_every_required_column_has_an_example_value(importer):
    """Requirement 4.3: the template's example row must be a usable shape."""
    example = columns.example_row(importer)
    for spec in columns.columns_for(importer):
        if spec.required:
            assert example[spec.key], f"{importer}.{spec.key} needs an example"


@pytest.mark.parametrize("importer", SHEETS)
def test_examples_are_synthetic(importer):
    """Requirements 4.6, 14.6: no real site, device, serial, or customer values."""
    blob = " ".join(columns.example_row(importer).values()).lower()
    for real_ish in ("syd", "firmus", "lab1", "nvidia", "arista", "commscope", "spectrum"):
        assert real_ish not in blob
    assert "example" in blob or "exa" in blob


@pytest.mark.parametrize("importer", SHEETS)
def test_choice_columns_offer_at_least_two_values(importer):
    for spec in columns.columns_for(importer):
        if spec.choices:
            assert len(spec.choices) >= 2
            assert len(set(spec.choices)) == len(spec.choices)


def test_the_transceiver_column_does_not_exist(importer="cabling"):
    """Decision D3: dropped from iteration 1, so an uploaded one is an unrecognised column."""
    assert "transceiver" not in columns.spec_by_key(importer)


def test_primary_address_columns_use_the_requested_excel_headers():
    specs = columns.spec_by_key("devices")
    assert specs["primary_address_v4"].label == "primary address v4"
    assert specs["primary_address_v6"].label == "primary address v6"
    assert specs["primary_address_v4"].required is False
    assert specs["primary_address_v6"].required is False


def test_rack_form_factor_is_not_called_rack_type():
    """NetBox 4.2 gave `rack_type` to a foreign key pointing at a different model."""
    keys = columns.spec_by_key("hierarchy")
    assert "rack_form_factor" in keys
    assert "rack_type" not in keys


def test_unknown_importer_is_named_in_the_error():
    with pytest.raises(ValueError, match="unknown importer 'ipam'"):
        columns.columns_for("ipam")


def test_every_sheet_has_a_worksheet_title():
    assert set(columns.SHEET_TITLES) == set(columns.SHEETS)
