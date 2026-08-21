# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Cable profiles — what a cable carries inside, and how many terminations it can hold (D10).

A profile names the connectors and positions on each end of a cable. It is how NetBox 4.5+ expresses
a breakout, a trunk, or a polarity shuffle, and it is what lets NetBox trace one lane through a
cable instead of treating the cable as a single opaque hop.

Two jobs live in this module.

`PROFILES` is the value list, used for the xlsx picklist and the template's guidance row *only*.
Validation asks the API what it accepts, exactly as every other choice column does (Requirement
9.13), so a value a later NetBox adds is never rejected here for being new.

`structure()` reads the connector and position counts back out of a profile code, which is what
Requirement 9.19 compares a group's terminations against. That is a parse of somebody else's naming
convention rather than a documented API, so it is deliberately loose: every `<n>C<n>P` token is
taken, the first describing side A and the second side B, and a code carrying one token describes
both sides with it. `-shuffle` changes which A position reaches which B position, not how many of
either exist, so it is recorded and then ignored by the count check.

The risk in parsing a convention is that the convention changes. It is covered twice:
`test_cable_profiles.py` asserts every one of the 26 values NetBox 4.6.8 reports still parses, so a
rename fails a test here rather than an operator's import; and `structure()` returning None is the
runtime fallback, which Requirement 9.19 turns into a warning rather than a guessed limit.
"""

import re
from dataclasses import dataclass

# The 26 values `OPTIONS /api/dcim/cables/` reports on NetBox 4.6.8, in the order it reports them.
# Picklist and guidance material only — never the validation authority (Requirement 9.13).
PROFILES: tuple[str, ...] = (
    "single-1c1p",
    "single-1c2p",
    "single-1c4p",
    "single-1c6p",
    "single-1c8p",
    "single-1c12p",
    "single-1c16p",
    "trunk-2c1p",
    "trunk-2c2p",
    "trunk-2c4p",
    "trunk-2c4p-shuffle",
    "trunk-2c6p",
    "trunk-2c8p",
    "trunk-2c12p",
    "trunk-4c1p",
    "trunk-4c2p",
    "trunk-4c4p",
    "trunk-4c4p-shuffle",
    "trunk-4c6p",
    "trunk-4c8p",
    "trunk-8c4p",
    "breakout-1c2p-2c1p",
    "breakout-1c4p-4c1p",
    "breakout-1c6p-6c1p",
    "breakout-1c8p-8c1p",
    "breakout-2c4p-8c1p-shuffle",
)

_TOKEN = re.compile(r"(\d+)c(\d+)p")

SHUFFLE = "shuffle"


@dataclass(frozen=True)
class ProfileStructure:
    """How many connectors and positions a profile declares on each side."""

    code: str
    a_connectors: int
    a_positions: int
    b_connectors: int
    b_positions: int
    shuffle: bool = False

    def connectors(self, side: str) -> int:
        """The most terminations this side can carry.

        One termination occupies one connector, so the connector count is the limit Requirement
        9.19 enforces. Positions live *inside* a connector and are NetBox's business, not a limit
        on how many rows may name this side.
        """
        return self.a_connectors if side == "a" else self.b_connectors

    def positions(self, side: str) -> int:
        return self.a_positions if side == "a" else self.b_positions

    def describe(self, side: str) -> str:
        connectors, positions = self.connectors(side), self.positions(side)
        return (
            f"{connectors} connector{'s' if connectors != 1 else ''} "
            f"of {positions} position{'s' if positions != 1 else ''}"
        )


def structure(code: str) -> ProfileStructure | None:
    """The connector/position shape of a profile code, or None when it cannot be read.

    None is not an error: it means this tool does not recognise the shape of a value NetBox
    accepts, which Requirement 9.19 reports as a warning rather than treating as a limit of zero.
    """
    folded = str(code or "").strip().casefold()
    tokens = _TOKEN.findall(folded)
    if not tokens:
        return None
    a_side = tokens[0]
    b_side = tokens[1] if len(tokens) > 1 else tokens[0]
    return ProfileStructure(
        code=folded,
        a_connectors=int(a_side[0]),
        a_positions=int(a_side[1]),
        b_connectors=int(b_side[0]),
        b_positions=int(b_side[1]),
        shuffle=SHUFFLE in folded,
    )
