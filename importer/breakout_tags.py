# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Approved profile-position factors and their pre-existing NetBox tag slugs (D15)."""

BREAKOUT_TAGS: dict[int, str] = {
    2: "port-breakout-2x",
    4: "port-breakout-4x",
    8: "port-breakout-8x",
}

BREAKOUT_TAG_FAMILY = frozenset(BREAKOUT_TAGS.values())
TAGS_ENDPOINT = "extras.tags"
