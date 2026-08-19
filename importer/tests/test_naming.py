# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 10 — the rack naming convention. Requirements 6.1, 6.2, 6.4, 6.5."""

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
        assert error.expected == "<datacenter>-<datahall>-<rack>"
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
        assert naming.expected_pattern() == "<datacenter>_<datahall>_<rack>"

    def test_an_explicit_delimiter_overrides_the_setting(self):
        parsed = naming.parse_rack_name("EXA1/DH1/R01", "/")
        assert parsed.datahall == "DH1"


class TestComparison:
    def test_lookup_is_case_insensitive_while_creation_keeps_the_supplied_casing(self):
        """Requirement 6.4: compare folded, create with what the sheet said."""
        parsed = naming.parse_rack_name("Exa1-Dh1-r01")
        assert parsed.key == naming.parse_rack_name("EXA1-DH1-R01").key
        assert parsed.raw == "Exa1-Dh1-r01"

    def test_agreement_with_its_own_columns_is_case_insensitive(self):
        """Requirement 7.3 leans on this to catch a rack name naming another datacenter."""
        parsed = naming.parse_rack_name("EXA1-DH1-R01")
        assert parsed.agrees_with(datacenter="exa1", datahall="dh1")
        assert not parsed.agrees_with(datacenter="EXA9", datahall="DH1")
        assert not parsed.agrees_with(datacenter="EXA1", datahall="DH9")


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
