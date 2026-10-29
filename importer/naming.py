# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Opaque rack-name keys and NetBox slug validation (D16).

Rack names are natural keys, not encoded paths. The complete value is trimmed, preserved for create,
and case-folded only for comparison. Site/location ancestry comes from NetBox relationships.

Slug derivation remains here for object types outside the D16 hierarchy sheet that still need it.
"""

import re
from dataclasses import dataclass

from django.utils.text import slugify

from importer.validation import Code, RowError

# NetBox's slug fields are 100 characters and validate against this charset, uppercase included.
SLUG_MAX_LENGTH = 100
SLUG_CHARSET = re.compile(r"[-a-zA-Z0-9_]+")
SLUG_CHARSET_DESCRIPTION = "letters, digits, hyphen and underscore only"


@dataclass(frozen=True)
class RackName:
    """An opaque rack name, keeping the casing the sheet supplied (Requirement 6.2)."""

    raw: str

    @property
    def key(self) -> str:
        """Case-folded form used for lookups and comparisons, never for creation."""
        return fold(self.raw)


def fold(value: str) -> str:
    """Opaque natural keys compare case-insensitively."""
    return value.strip().casefold()


def validate_rack_name(value: str, *, column: str = "rack") -> RackName:
    """Validate a non-empty opaque rack name without interpreting punctuation."""
    raw = (value or "").strip()
    if not raw:
        raise RowError(
            Code.RACK_NAME_MALFORMED,
            column=column,
            value=raw,
            expected="a non-empty NetBox rack name",
            message="A rack name cannot be blank.",
        )
    return RackName(raw=raw)


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
) -> str:
    """Validate NetBox's complete slug contract. Raises `RowError` otherwise."""
    slug = (value or "").strip()

    if len(slug) > SLUG_MAX_LENGTH:
        raise RowError(
            Code.SLUG_INVALID,
            column=column,
            value=slug,
            expected=f"at most {SLUG_MAX_LENGTH} characters",
            message=(
                f"{slug!r} is {len(slug)} characters. NetBox allows "
                f"{SLUG_MAX_LENGTH}. Correct the slug."
            ),
        )

    if not SLUG_CHARSET.fullmatch(slug):
        raise RowError(
            Code.SLUG_INVALID,
            column=column,
            value=slug,
            expected=SLUG_CHARSET_DESCRIPTION,
            message=(
                f"{slug!r} is not a valid NetBox slug: "
                f"{SLUG_CHARSET_DESCRIPTION}. Correct the slug."
            ),
        )

    return slug
