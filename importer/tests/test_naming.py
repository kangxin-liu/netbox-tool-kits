# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Tasks 10 and 18a — the rack naming convention and the slugs it is built from.

Requirements 6.1, 6.2, 6.4, 6.5, 6.6, 6.7, 7.10, 7.11.
"""

import pytest
from django.test import override_settings

from importer import naming
from importer.validation import Code, RowError


class TestParsing:
    def test_a_three_segment_name_splits_into_its_parts(self):
        parsed = naming.parse_rack_name("EXA1-DH1-R01")
        assert (parsed.datacenter, parsed.datahall, parsed.rack) == ("EXA1", "DH1", "R01")
        assert parsed.raw == "EXA1-DH1-R01"

    @pytest.mark.parametrize("value", ["EXA1-R01", "EXA1", "EXA1-DH1-R01-A", "EXA1-DH1-R01-A-B"])
    def test_the_wrong_segment_count_is_rejected(self, value):
        with pytest.raises(RowError) as raised:
            naming.parse_rack_name(value)
        assert raised.value.code is Code.RACK_NAME_MALFORMED

    @pytest.mark.parametrize("value", ["EXA1--R01", "-DH1-R01", "EXA1-DH1-", "  -  -  "])
    def test_an_empty_segment_is_rejected(self, value):
        with pytest.raises(RowError):
            naming.parse_rack_name(value)

    def test_the_error_quotes_the_received_value_and_the_expected_pattern(self):
        """Requirement 6.2, and Requirement 16.5: an engineer can fix the sheet from the message."""
        with pytest.raises(RowError) as raised:
            naming.parse_rack_name("EXA1-R01")
        error = raised.value
        assert error.value == "EXA1-R01"
        assert error.expected == "<datacenter_slug>-<datahall_slug>-<rack>"
        assert "EXA1-R01" in error.message
        assert error.column == "rack"

    def test_surrounding_whitespace_is_trimmed(self):
        parsed = naming.parse_rack_name("  EXA1 - DH1 - R01  ")
        assert (parsed.datacenter, parsed.datahall, parsed.rack) == ("EXA1", "DH1", "R01")

    def test_a_blank_name_is_rejected_rather_than_crashing(self):
        with pytest.raises(RowError):
            naming.parse_rack_name("")


class TestDelimiter:
    @override_settings(RACK_NAME_DELIMITER="_")
    def test_the_delimiter_comes_from_settings(self):
        parsed = naming.parse_rack_name("EXA1_DH1_R01")
        assert parsed.rack == "R01"
        # And the default delimiter is no longer special.
        with pytest.raises(RowError):
            naming.parse_rack_name("EXA1-DH1-R01")

    @override_settings(RACK_NAME_DELIMITER="_")
    def test_the_expected_pattern_follows_the_delimiter(self):
        assert naming.expected_pattern() == "<datacenter_slug>_<datahall_slug>_<rack>"

    def test_an_explicit_delimiter_overrides_the_setting(self):
        parsed = naming.parse_rack_name("EXA1/DH1/R01", "/")
        assert parsed.datahall == "DH1"


class TestComparison:
    def test_lookup_is_case_insensitive_while_creation_keeps_the_supplied_casing(self):
        """Requirement 6.4: compare folded, create with what the sheet said."""
        parsed = naming.parse_rack_name("Exa1-Dh1-r01")
        assert parsed.key == naming.parse_rack_name("EXA1-DH1-R01").key
        assert parsed.raw == "Exa1-Dh1-r01"

    def test_agreement_with_its_own_slugs_is_case_insensitive(self):
        """Requirement 6.4: `EXA1-DH1-R01` is the rack of the sites slugged `exa1` and `dh1`."""
        parsed = naming.parse_rack_name("EXA1-DH1-R01")
        assert parsed.agrees_with(datacenter_slug="exa1", datahall_slug="dh1")
        assert not parsed.agrees_with(datacenter_slug="exa9", datahall_slug="dh1")
        assert not parsed.agrees_with(datacenter_slug="exa1", datahall_slug="dh9")


class TestBuilding:
    def test_a_name_is_built_from_its_parts(self):
        assert naming.build_rack_name("EXA1", "DH1", "R01") == "EXA1-DH1-R01"

    def test_building_trims_each_part(self):
        assert naming.build_rack_name(" EXA1 ", " DH1", "R01 ") == "EXA1-DH1-R01"

    @override_settings(RACK_NAME_DELIMITER="_")
    def test_building_follows_the_configured_delimiter(self):
        assert naming.build_rack_name("EXA1", "DH1", "R01") == "EXA1_DH1_R01"

    def test_build_and_parse_round_trip(self):
        built = naming.build_rack_name("EXA1", "DH2", "R14")
        assert naming.parse_rack_name(built).datahall == "DH2"


class TestSlugValidation:
    """Requirement 6.6: NetBox's charset, minus the character a rack name is split on."""

    @pytest.mark.parametrize("value", ["exa1", "EXA1", "exa_1", "exa1_dh1", "x", "0", "a_B_9"])
    def test_the_netbox_charset_is_accepted(self, value):
        assert naming.validate_slug(value, column="datacenter_slug") == value

    @pytest.mark.parametrize("value", ["exa 1", "exa.1", "exa/1", "exá1", "exa!", ""])
    def test_a_value_outside_the_charset_is_rejected_as_not_a_slug(self, value):
        with pytest.raises(RowError) as raised:
            naming.validate_slug(value, column="datacenter_slug")
        assert raised.value.code is Code.SLUG_INVALID
        assert raised.value.column == "datacenter_slug"

    def test_a_hyphen_is_rejected_because_the_rack_name_is_split_on_it(self):
        with pytest.raises(RowError) as raised:
            naming.validate_slug("exa-1", column="datacenter_slug")
        error = raised.value
        assert error.code is Code.SLUG_CONTAINS_DELIMITER
        # The two mistakes are told apart by their message, which is the whole point of two checks.
        assert "'_'" in error.message
        assert error.expected == "a slug without '-'"

    def test_an_over_length_slug_is_rejected_with_both_numbers(self):
        with pytest.raises(RowError) as raised:
            naming.validate_slug("e" * 101, column="datacenter_slug")
        assert raised.value.code is Code.SLUG_INVALID
        assert "101" in raised.value.message
        assert "100" in raised.value.message

    def test_a_slug_of_exactly_the_maximum_length_is_accepted(self):
        value = "e" * naming.SLUG_MAX_LENGTH
        assert naming.validate_slug(value, column="datacenter_slug") == value

    @override_settings(RACK_NAME_DELIMITER="_")
    def test_the_delimiter_setting_flips_which_character_is_forbidden(self):
        """Requirement 6.7: the ban follows `RACK_NAME_DELIMITER`, not a literal hyphen."""
        assert naming.validate_slug("exa-1", column="datacenter_slug") == "exa-1"
        with pytest.raises(RowError) as raised:
            naming.validate_slug("exa_1", column="datacenter_slug")
        assert raised.value.code is Code.SLUG_CONTAINS_DELIMITER


class TestEffectiveSlug:
    """Requirement 7.10: one value, used for the lookup, the payload, and the rack name alike."""

    def effective(self, name, supplied):
        return naming.effective_slug(
            name, supplied, column="datacenter_slug", name_column="datacenter"
        )

    def test_a_supplied_slug_wins_over_the_name(self):
        assert self.effective("Example One", "exa1") == "exa1"

    def test_a_supplied_slug_keeps_its_casing(self):
        """Assumption 8: NetBox accepts uppercase, and the sheet's value is not folded."""
        assert self.effective("Example One", "EXA1") == "EXA1"

    def test_surrounding_whitespace_is_trimmed_from_a_supplied_slug(self):
        assert self.effective("Example One", "  exa1 ") == "exa1"

    def test_a_blank_slug_falls_back_to_one_derived_from_the_name(self):
        """The pre-D9 behaviour, which is what keeps a single-token sheet working untouched."""
        assert self.effective("EXA1", "") == "exa1"

    def test_a_derived_slug_containing_the_delimiter_is_reported_on_the_name_column(self):
        """Requirement 7.11: the fix is to fill the slug column in, so say so, there."""
        with pytest.raises(RowError) as raised:
            self.effective("Example One", "")
        error = raised.value
        assert error.code is Code.SLUG_CONTAINS_DELIMITER
        assert error.column == "datacenter"
        assert error.value == "example-one"
        assert "'datacenter_slug'" in error.message

    def test_a_name_with_nothing_sluggable_in_it_is_rejected(self):
        with pytest.raises(RowError) as raised:
            self.effective("—", "")
        assert raised.value.code is Code.SLUG_NOT_DERIVABLE
        assert raised.value.column == "datacenter"

    def test_a_supplied_slug_rescues_a_name_no_slug_can_be_derived_from(self):
        assert self.effective("—", "exa1") == "exa1"

    def test_an_invalid_supplied_slug_is_reported_on_the_slug_column(self):
        with pytest.raises(RowError) as raised:
            self.effective("Example One", "exa 1")
        assert raised.value.column == "datacenter_slug"


def test_nothing_else_in_the_app_splits_a_rack_name():
    """Requirement 6.5: the convention lives in exactly one module.

    A crude check, but it is the one that would have caught a second implementation appearing in an
    importer during a hurried change.
    """
    from pathlib import Path

    app = Path(__file__).resolve().parents[1]
    offenders = []
    for path in app.rglob("*.py"):
        if path.name in {"naming.py"} or "tests" in path.parts:
            continue
        source = path.read_text()
        if "RACK_NAME_DELIMITER" in source or ".split(delimiter" in source:
            offenders.append(path.name)
    assert offenders == []
