# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.

import pytest

from importer import naming
from importer.validation import Code, RowError


class TestOpaqueRackName:
    @pytest.mark.parametrize(
        "value",
        [
            "R01",
            "EXA01-MMR1-RK0001",
            "free_form_rack",
            "rack-with-many-segments-and-underscores_1",
        ],
    )
    def test_any_non_empty_name_is_opaque(self, value):
        rack = naming.validate_rack_name(value)
        assert rack.raw == value

    def test_surrounding_whitespace_is_trimmed(self):
        assert naming.validate_rack_name("  R01  ").raw == "R01"

    def test_casefolded_key_does_not_change_create_casing(self):
        rack = naming.validate_rack_name("ExA-R01")
        assert rack.raw == "ExA-R01"
        assert rack.key == naming.validate_rack_name("EXA-R01").key

    def test_blank_name_is_rejected(self):
        with pytest.raises(RowError) as raised:
            naming.validate_rack_name("  ")
        assert raised.value.code is Code.RACK_NAME_MALFORMED


class TestSlugValidation:
    @pytest.mark.parametrize(
        "slug",
        ["exa", "EXA01", "bth01-mmr1", "bth01_mmr1", "a-b_c-123"],
    )
    def test_full_netbox_charset_is_accepted(self, slug):
        assert naming.validate_slug(slug, column="slug") == slug

    @pytest.mark.parametrize("slug", ["", "has space", "slash/value", "emoji-😀"])
    def test_values_outside_netbox_charset_are_rejected(self, slug):
        with pytest.raises(RowError) as raised:
            naming.validate_slug(slug, column="slug")
        assert raised.value.code is Code.SLUG_INVALID

    def test_netbox_length_limit_is_enforced(self):
        assert naming.validate_slug("a" * 100, column="slug") == "a" * 100
        with pytest.raises(RowError):
            naming.validate_slug("a" * 101, column="slug")


def test_slug_derivation_remains_available_for_other_importers():
    assert naming.derive_slug("Example Tenant") == "example-tenant"
