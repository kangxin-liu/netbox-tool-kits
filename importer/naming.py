# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""How names are shaped: the rack naming convention, and slug derivation.

The rack convention is `<datacenter>-<datahall>-<rack>`, e.g. `EXA1-DH1-R01`. This is the only
module that knows it (Requirement 6.5), so changing the delimiter or the segment count is a
single-file change. Nothing else in the codebase splits a rack name. The delimiter comes from
`RACK_NAME_DELIMITER` and defaults to `-`.

Slugs live here for the same reason. NetBox requires one on regions, sites, locations, device types,
and manufacturers, and no sheet asks for one, so every importer derives it from the name
(Requirement 7.3) — and they must all derive it identically, or two importers would disagree about
which existing object a name refers to.
"""

from dataclasses import dataclass

from django.conf import settings
from django.utils.text import slugify

from importer.validation import Code, RowError

# NetBox's slug fields are 100 characters.
SLUG_MAX_LENGTH = 100

SEGMENT_COUNT = 3
PATTERN = "<datacenter>-<datahall>-<rack>"


def delimiter() -> str:
    return settings.RACK_NAME_DELIMITER


@dataclass(frozen=True)
class RackName:
    """A parsed rack name, keeping the casing the sheet supplied (Requirement 6.4)."""

    datacenter: str
    datahall: str
    rack: str
    raw: str

    @property
    def key(self) -> str:
        """Case-folded form used for lookups and comparisons, never for creation."""
        return fold(self.raw)

    def agrees_with(self, *, datacenter: str, datahall: str) -> bool:
        return fold(self.datacenter) == fold(datacenter) and fold(self.datahall) == fold(datahall)


def fold(value: str) -> str:
    """Rack names compare case-insensitively (Requirement 6.4)."""
    return value.strip().casefold()


def expected_pattern(delim: str | None = None) -> str:
    delim = delimiter() if delim is None else delim
    if delim == "-":
        return PATTERN
    return PATTERN.replace("-", delim)


def parse_rack_name(value: str, delim: str | None = None, *, column: str = "rack") -> RackName:
    """Split into exactly three non-empty segments, or raise `RowError`.

    Raising rather than returning None keeps the caller's happy path readable; the importers convert
    the error into a located `ValidationIssue`.
    """
    delim = delimiter() if delim is None else delim
    raw = (value or "").strip()
    segments = raw.split(delim)

    if len(segments) != SEGMENT_COUNT or not all(segment.strip() for segment in segments):
        raise RowError(
            Code.RACK_NAME_MALFORMED,
            column=column,
            value=raw,
            expected=expected_pattern(delim),
            message=(
                f"Rack name {raw!r} does not read {expected_pattern(delim)}: expected exactly "
                f"{SEGMENT_COUNT} non-empty segments separated by {delim!r}, "
                f"found {len(segments)}."
            ),
        )

    datacenter, datahall, rack = (segment.strip() for segment in segments)
    return RackName(datacenter=datacenter, datahall=datahall, rack=rack, raw=raw)


def build_rack_name(datacenter: str, datahall: str, rack: str, delim: str | None = None) -> str:
    delim = delimiter() if delim is None else delim
    return delim.join(part.strip() for part in (datacenter, datahall, rack))


def derive_slug(name: str) -> str:
    """The slug NetBox requires, derived from a name (Requirement 7.3).

    Returns an empty string for a name with nothing slug-able in it, which the importers report as
    a rejected row rather than sending NetBox a blank slug.
    """
    return slugify(name)[:SLUG_MAX_LENGTH]
