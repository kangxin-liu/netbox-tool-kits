# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 24 — reading connector counts out of a cable profile code (Requirement 9.19, D10).

The list in `cable_profiles.PROFILES` is what `OPTIONS /api/dcim/cables/` reported on NetBox 4.6.8.
`structure()` parses a naming convention rather than a documented API, so the first test here is the
guard that matters: if a later NetBox renames its profiles, it fails *here* rather than in an
operator's import.
"""

import pytest

from importer.cable_profiles import PROFILES, structure


class TestEveryReportedProfileParses:
    """The convention guard (design.md, "Cable profiles: the 26 values")."""

    def test_the_probed_catalogue_is_all_26_values(self):
        assert len(PROFILES) == len(set(PROFILES)) == 26

    @pytest.mark.parametrize("code", PROFILES)
    def test_each_one_yields_connector_and_position_counts(self, code):
        shape = structure(code)
        assert shape is not None, f"{code} no longer parses: the naming convention has changed"
        for side in ("a", "b"):
            assert shape.connectors(side) >= 1
            assert shape.positions(side) >= 1

    @pytest.mark.parametrize("code", PROFILES)
    def test_a_breakout_fans_out_and_nothing_else_does(self, code):
        """The families are structural, not decorative: only a breakout is asymmetric."""
        shape = structure(code)
        asymmetric = shape.connectors("a") != shape.connectors("b")
        assert asymmetric is code.startswith("breakout-")


class TestStructure:
    def test_a_single_profile_is_symmetrical(self):
        shape = structure("single-1c2p")
        assert (shape.a_connectors, shape.a_positions) == (1, 2)
        assert (shape.b_connectors, shape.b_positions) == (1, 2)

    def test_a_trunk_repeats_its_one_token_on_both_sides(self):
        shape = structure("trunk-2c4p")
        assert (shape.a_connectors, shape.b_connectors) == (2, 2)
        assert (shape.a_positions, shape.b_positions) == (4, 4)

    def test_a_breakout_reads_the_second_token_as_the_b_side(self):
        """`1c4p-4c1p`: one connector of four positions fanning out to four of one."""
        shape = structure("breakout-1c4p-4c1p")
        assert (shape.a_connectors, shape.a_positions) == (1, 4)
        assert (shape.b_connectors, shape.b_positions) == (4, 1)

    def test_the_two_way_breakout_a_400g_sr8_needs(self):
        """One SR8 cage to two SR4 links: two far ends, so `1c2p-2c1p`."""
        shape = structure("breakout-1c2p-2c1p")
        assert shape.connectors("a") == 1
        assert shape.connectors("b") == 2

    def test_a_shuffle_breakout_keeps_both_tokens_and_records_the_shuffle(self):
        shape = structure("breakout-2c4p-8c1p-shuffle")
        assert (shape.a_connectors, shape.a_positions) == (2, 4)
        assert (shape.b_connectors, shape.b_positions) == (8, 1)
        assert shape.shuffle is True

    def test_a_shuffle_suffix_does_not_change_the_counts(self):
        """Shuffle remaps which position reaches which, not how many there are."""
        plain, shuffled = structure("trunk-2c4p"), structure("trunk-2c4p-shuffle")
        assert plain.connectors("a") == shuffled.connectors("a")
        assert plain.positions("b") == shuffled.positions("b")
        assert (plain.shuffle, shuffled.shuffle) == (False, True)

    @pytest.mark.parametrize(
        ("code", "positions"),
        [("single-1c12p", 12), ("single-1c16p", 16), ("trunk-2c12p", 12)],
    )
    def test_a_two_digit_position_count_is_read_whole(self, code, positions):
        """`1c16p` is sixteen positions, not one followed by a stray six."""
        assert structure(code).positions("a") == positions

    @pytest.mark.parametrize("code", ["BREAKOUT-1C4P-4C1P", "  breakout-1c4p-4c1p  "])
    def test_case_and_surrounding_space_do_not_matter(self, code):
        assert structure(code).connectors("b") == 4

    @pytest.mark.parametrize("code", ["", "   ", "mystery", "custom-profile", "1cp", None])
    def test_a_code_with_no_countable_tokens_returns_none(self, code):
        """None is 'this tool cannot read it', which Requirement 9.19 warns about rather than
        turning into a limit of zero and blocking a cable NetBox would accept."""
        assert structure(code) is None


class TestDescribe:
    def test_it_reads_as_a_sentence_fragment_for_an_error_message(self):
        shape = structure("breakout-1c4p-4c1p")
        assert shape.describe("a") == "1 connector of 4 positions"
        assert shape.describe("b") == "4 connectors of 1 position"
