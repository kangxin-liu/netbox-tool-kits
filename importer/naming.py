# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""How names are shaped: the rack naming convention, and the slugs its first two segments are.

The rack convention is `<datacenter_slug>-<datahall_slug>-<rack>`, e.g. `EXA1-DH1-R01`. This is the
only module that knows it (Requirement 6.5), so changing the delimiter or the segment count is a
single-file change. Nothing else in the codebase splits a rack name. The delimiter comes from
`RACK_NAME_DELIMITER` and defaults to `-`.

The first two segments are *slugs*, not names (decision D9). Deriving the slug from the name was
fine while the slug was invisible plumbing, but it forced the name to double as the short code: a
site had to be called `EXA1` rather than "Example One" purely so the rack name would come out short.
The hierarchy sheet therefore supplies the two slugs in their own optional columns, falling back to
a slug derived from the name where a cell is blank (Requirement 7.10).

Slugs live in this module for the same reason the convention does. NetBox requires one on regions,
sites, locations, device types, and manufacturers; only the hierarchy sheet asks for one, and only
for two of its three levels, so everywhere else it is derived from the name (Requirement 7.3) — and
it has to be derived *identically*, or two importers would disagree about which existing object a
given name refers to.
"""

import re
from dataclasses import dataclass

from django.conf import settings
from django.utils.text import slugify

from importer.validation import Code, RowError

# NetBox's slug fields are 100 characters and validate against this charset, uppercase included.
SLUG_MAX_LENGTH = 100
SLUG_CHARSET = re.compile(r"[-a-zA-Z0-9_]+")
SLUG_CHARSET_DESCRIPTION = "letters, digits, hyphen and underscore only"

SEGMENT_COUNT = 3
PATTERN = "<datacenter_slug>-<datahall_slug>-<rack>"


def delimiter() -> str:
    return settings.RACK_NAME_DELIMITER


@dataclass(frozen=True)
class RackName:
    """A parsed rack name, keeping the casing the sheet supplied (Requirement 6.4)."""

    datacenter: str
    """Segment 1 — the datacenter's slug, not its name (D9)."""

    datahall: str
    """Segment 2 — the datahall's slug."""

    rack: str
    raw: str

    @property
    def key(self) -> str:
        """Case-folded form used for lookups and comparisons, never for creation."""
        return fold(self.raw)

    def agrees_with(self, *, datacenter_slug: str, datahall_slug: str) -> bool:
        """Requirement 6.3, against the row's *effective* slugs rather than its names."""
        return fold(self.datacenter) == fold(datacenter_slug) and fold(self.datahall) == fold(
            datahall_slug
        )


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


def validate_slug(
    value: str,
    *,
    column: str,
    delim: str | None = None,
    derived_from: str = "",
    advice: str = "",
) -> str:
    """NetBox's charset, minus the delimiter. Raises `RowError` otherwise.

    Two checks rather than one narrowed regex, because the two mistakes have different fixes and an
    operator needs to be told which one they made. NetBox accepts `^[-a-zA-Z0-9_]+$` and the rack
    name is split on `-`, so the rules disagree about exactly one character: a value outside the
    charset is not a slug at all, while `exa-1` is a perfectly good NetBox slug that this tool
    cannot build a rack name from. Collapsing both into one message would be wrong for one of them.

    The delimiter is read from settings rather than matched as a literal `-`, so configuring `_`
    forbids `_` and permits `-` without editing this rule (Requirement 6.7).

    `derived_from` and `advice` only change the wording, for the case where the value came from a
    blank slug column falling back to the name: the operator's fix there is to fill the slug column
    in, and a message that just says "invalid slug" about a cell they never typed in would not tell
    them that.
    """
    delim = delimiter() if delim is None else delim
    slug = (value or "").strip()
    origin = f"The slug derived from {derived_from!r} is {slug!r}" if derived_from else f"{slug!r}"
    remedy = advice or "Correct the slug."

    if len(slug) > SLUG_MAX_LENGTH:
        raise RowError(
            Code.SLUG_INVALID,
            column=column,
            value=slug,
            expected=f"at most {SLUG_MAX_LENGTH} characters",
            message=(
                f"{origin}, which is {len(slug)} characters. NetBox allows "
                f"{SLUG_MAX_LENGTH}. {remedy}"
            ),
        )

    if not SLUG_CHARSET.fullmatch(slug):
        raise RowError(
            Code.SLUG_INVALID,
            column=column,
            value=slug,
            expected=SLUG_CHARSET_DESCRIPTION,
            message=(
                f"{origin}, which is not a valid NetBox slug: {SLUG_CHARSET_DESCRIPTION}. {remedy}"
            ),
        )

    if delim in slug:
        alternative = "_" if delim != "_" else "-"
        raise RowError(
            Code.SLUG_CONTAINS_DELIMITER,
            column=column,
            value=slug,
            expected=f"a slug without {delim!r}",
            message=(
                f"{origin}, which contains {delim!r}. A rack name reads "
                f"{expected_pattern(delim)} and is split on {delim!r}, so a slug containing it "
                f"would make the segments ambiguous. Use {alternative!r} instead. {remedy}"
            ),
        )

    return slug


def effective_slug(
    name: str,
    supplied: str,
    *,
    column: str,
    name_column: str,
    delim: str | None = None,
) -> str:
    """The one slug a level is looked up by, created with, and compared on (Requirement 7.10).

    Supplied when its column is non-blank, derived from the name otherwise. Both names are needed
    because the fallback can itself be invalid — `derive_slug("Data Hall 1")` is `data-hall-1`,
    which contains the delimiter. That row has to be rejected, but pointing the error at a cell the
    operator deliberately left blank tells them nothing, so a derived slug is reported against the
    *name* column with a message that says to fill the slug column in (Requirement 7.11).
    """
    given = (supplied or "").strip()
    if given:
        return validate_slug(given, column=column, delim=delim)

    derived = derive_slug(name)
    if not derived:
        raise RowError(
            Code.SLUG_NOT_DERIVABLE,
            column=name_column,
            value=name,
            expected="a name containing at least one letter or digit",
            message=(
                f"No URL-safe slug can be derived from {name!r}, and NetBox requires one. Give it "
                f"a name with letters or digits, or put a slug in the {column!r} column."
            ),
        )
    return validate_slug(
        derived,
        column=name_column,
        delim=delim,
        derived_from=name,
        advice=f"Put a slug in the {column!r} column.",
    )
