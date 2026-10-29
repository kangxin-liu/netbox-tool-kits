# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""The sheet contract: one definition per column, read by template generation, parsing, and
validation alike, so a downloaded template cannot drift from what the parser accepts (Req 4.2).

Every column declares a `Conflict`, with no default. That is deliberate: adding a column forces an
explicit decision about what it means when the sheet disagrees with an object that already exists in
NetBox (Requirement 11.5c, decision D8).

Choice values are *not* hardcoded here. `choices` names the NetBox field whose choices come from the
API at validation time (Requirement 7.7); the tuples below carry only the values used to build the
xlsx picklists and to write example rows, and validation always defers to what the API reports.
"""

from dataclasses import dataclass
from enum import Enum

from importer.cable_profiles import PROFILES as CABLE_PROFILES

# The template's guidance line sits on line 2 and starts with this marker so the parser can drop it
# without treating it as data (Requirements 4.7, 5.11).
GUIDANCE_MARKER = "# guidance:"

# Picklist material for `*_interface_type` (D12). A representative slice of NetBox's interface-type
# choices rather than all ~180 of them: the full list would make the xlsx dropdown unusable, and
# validation asks the API regardless (Requirement 7.7), so a type missing from here is still
# accepted. Ordered by speed, because that is how somebody scanning the list looks for one.
INTERFACE_TYPES: tuple[str, ...] = (
    "1000base-t",
    "10gbase-t",
    "10gbase-x-sfpp",
    "25gbase-x-sfp28",
    "40gbase-x-qsfpp",
    "50gbase-x-sfp56",
    "100gbase-x-qsfp28",
    "200gbase-x-qsfp56",
    "400gbase-x-qsfpdd",
    "800gbase-x-osfp",
)

INTERFACE_TYPE_HELP = (
    "Used only to create an unknown physical interface: the port itself normally, or the parent "
    "when *_parent is filled. Virtual breakout children always use type virtual"
)

PARENT_HELP = (
    "Optional breakout parent interface on the same *_end device. When filled, *_port is a virtual "
    "child and the cable terminates on this physical parent"
)


class Conflict(Enum):
    """What it means when this column disagrees with an object that already exists (D8)."""

    CONSEQUENTIAL = "error"
    """Later rows are built on this value, so a mismatch blocks the whole file."""

    DESCRIPTIVE = "warning"
    """Nothing downstream reads it: the operator acknowledges the difference and carries on."""

    IDENTITY = "none"
    """Part of the natural key. It cannot differ — a different value means a different object."""


@dataclass(frozen=True)
class ColumnSpec:
    key: str
    """snake_case identity used after header whitespace and hyphens are normalised."""

    label: str
    """Exact header text generated in CSV and Excel templates."""
    required: bool
    conflict: Conflict
    help_text: str
    choices: tuple[str, ...] = ()
    example: str = ""

    numeric: bool = False
    """Compared as a number, not as text (Requirement 11.5b).

    Set where NetBox may return `45` or `45.0` for what the sheet wrote as `45`: comparing those as
    strings would report a conflict that does not exist.
    """

    @property
    def guidance(self) -> str:
        """The cell written into the template's guidance row."""
        parts = ["required" if self.required else "optional", self.help_text]
        if self.choices:
            parts.append("one of: " + ", ".join(self.choices))
        return " — ".join(part for part in parts if part)


# --- Sheet 1: hierarchy, one row per rack ---------------------------------------------------
# D16 treats the site and parent location as prepared prerequisites. The importer may create only
# their direct child datahall and its racks. Prerequisite slugs and names are identities; the child
# slug is consequential because an existing child may first be matched by name.
HIERARCHY_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        key="site_slug",
        label="site_slug",
        required=True,
        conflict=Conflict.IDENTITY,
        help_text="Slug of an existing NetBox site. This importer never creates the site",
        example="exa",
    ),
    ColumnSpec(
        key="parent_location_slug",
        label="parent_location_slug",
        required=True,
        conflict=Conflict.IDENTITY,
        help_text=(
            "Slug of an existing parent location in the site. This importer never creates or "
            "moves the parent"
        ),
        example="exa01",
    ),
    ColumnSpec(
        key="datahall",
        label="datahall",
        required=True,
        conflict=Conflict.IDENTITY,
        help_text="Child location name, resolved or created directly under the parent location",
        example="EXA01-MMR1",
    ),
    ColumnSpec(
        key="datahall_slug",
        label="datahall_slug",
        required=True,
        conflict=Conflict.CONSEQUENTIAL,
        help_text=(
            "Child location slug. Required; letters, digits, hyphen and underscore are accepted. "
            "Hierarchy comes from NetBox relationships, not from this text"
        ),
        example="exa01_mmr1",
    ),
    ColumnSpec(
        key="rack",
        label="rack",
        required=True,
        conflict=Conflict.IDENTITY,
        help_text="Opaque NetBox rack name. Hyphens and underscores have no hierarchy meaning",
        example="EXA01-MMR1-RK0001",
    ),
    ColumnSpec(
        key="rack_u_height",
        label="rack_u_height",
        required=True,
        conflict=Conflict.CONSEQUENTIAL,
        help_text="Rack height in units, a positive integer. Device positions are checked on it",
        example="45",
        numeric=True,
    ),
    ColumnSpec(
        key="rack_width",
        label="rack_width",
        required=False,
        conflict=Conflict.CONSEQUENTIAL,
        help_text="Rail width in inches. Defaults to 19",
        choices=("10", "19", "21", "23"),
        example="19",
        numeric=True,
    ),
    ColumnSpec(
        key="rack_status",
        label="rack_status",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Defaults to active. Validated against the choices NetBox reports",
        choices=("reserved", "available", "planned", "active", "deprecated"),
        example="active",
    ),
    ColumnSpec(
        key="rack_form_factor",
        label="rack_form_factor",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        # NetBox 4.2 renamed Rack.type to Rack.form_factor and gave `rack_type` to a foreign key
        # pointing at the separate RackType model, so the old name would aim at the wrong field.
        help_text="Physical form factor. Maps to NetBox's Rack.form_factor field",
        choices=(
            "2-post-frame",
            "4-post-frame",
            "4-post-cabinet",
            "wall-frame",
            "wall-frame-vertical",
            "wall-cabinet",
            "wall-cabinet-vertical",
        ),
        example="4-post-cabinet",
    ),
    ColumnSpec(
        key="tenant",
        label="tenant",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Name of an existing tenant. This importer never creates tenants",
        example="",
    ),
    ColumnSpec(
        key="description",
        label="description",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Free text, compared case- and whitespace-insensitively against NetBox",
        example="compute row",
    ),
)


# --- Sheet 2: devices ----------------------------------------------------------------------
# D11 adds four optional address columns. Interface names stay on the device type
# (`mgmt_only` / type `loopback`); the sheet carries CIDR addresses only.
DEVICE_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        key="rack",
        label="rack",
        required=True,
        conflict=Conflict.CONSEQUENTIAL,
        help_text="Existing rack name. This importer never creates racks",
        example="EXA1-DH1-R01",
    ),
    ColumnSpec(
        key="rack_face",
        label="rack_face",
        required=True,
        conflict=Conflict.CONSEQUENTIAL,
        help_text="Which face the device is mounted on",
        choices=("front", "rear"),
        example="front",
    ),
    ColumnSpec(
        key="bottom_ru",
        label="bottom_ru",
        required=True,
        conflict=Conflict.CONSEQUENTIAL,
        help_text="Lowest rack unit the device occupies, submitted as the device position",
        example="10",
        numeric=True,
    ),
    ColumnSpec(
        key="ru_height",
        label="ru_height",
        required=True,
        conflict=Conflict.CONSEQUENTIAL,
        help_text="Units the device occupies. Must match the device type's own height",
        example="1",
        numeric=True,
    ),
    ColumnSpec(
        key="device_name",
        label="device_name",
        required=True,
        conflict=Conflict.IDENTITY,
        help_text="Device name, unique within its scope. An existing name is skipped, not renamed",
        example="exa1-dh1-r01-leaf01",
    ),
    ColumnSpec(
        key="model",
        label="model",
        required=True,
        conflict=Conflict.CONSEQUENTIAL,
        help_text="Existing device type model. Rejected when unknown unless autocreate is enabled",
        example="Example-Switch-32C",
    ),
    ColumnSpec(
        key="serial",
        label="serial",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Free text. Submitted only when non-blank",
        example="EXAMPLE-SERIAL-0001",
    ),
    ColumnSpec(
        key="manufacturer",
        label="manufacturer",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Required only when NETBOX_AUTOCREATE_DEVICE_TYPES is on and the model is new",
        example="Example Networks",
    ),
    ColumnSpec(
        key="device_role",
        label="device_role",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Existing role. Falls back to NETBOX_DEFAULT_DEVICE_ROLE, else row rejected",
        example="leaf-switch",
    ),
    ColumnSpec(
        key="status",
        label="status",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Defaults to active. Validated against the choices NetBox reports",
        choices=(
            "offline",
            "active",
            "planned",
            "staged",
            "failed",
            "inventory",
            "decommissioning",
        ),
        example="active",
    ),
    ColumnSpec(
        key="asset_tag",
        label="asset_tag",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Unique across NetBox. A tag held by a different object blocks the row",
        example="",
    ),
    ColumnSpec(
        key="tenant",
        label="tenant",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Name of an existing tenant. This importer never creates tenants",
        example="",
    ),
    ColumnSpec(
        key="loopback_ip4",
        label="loopback_ip4",
        required=False,
        conflict=Conflict.CONSEQUENTIAL,
        help_text=(
            "IPv4 CIDR of the loopback (type=loopback on the device type). At least one of "
            "the four address cells is required on a type that defines those interfaces"
        ),
        example="192.0.2.1/32",
    ),
    ColumnSpec(
        key="loopback_ip6",
        label="loopback_ip6",
        required=False,
        conflict=Conflict.CONSEQUENTIAL,
        help_text="IPv6 CIDR of the same loopback. Prefix length is required, not defaulted",
        example="2001:db8::1/128",
    ),
    ColumnSpec(
        key="management_ip4",
        label="management_ip4",
        required=False,
        conflict=Conflict.CONSEQUENTIAL,
        help_text=(
            "IPv4 CIDR of the management port (mgmt_only on the device type). Wins as "
            "primary_ip4 over the loopback when filled"
        ),
        example="192.0.2.10/24",
    ),
    ColumnSpec(
        key="management_ip6",
        label="management_ip6",
        required=False,
        conflict=Conflict.CONSEQUENTIAL,
        help_text=(
            "IPv6 CIDR of the same management port. Becomes the device's primary only when "
            "no IPv4 is supplied"
        ),
        example="2001:db8:1::10/64",
    ),
    ColumnSpec(
        key="primary_address_v4",
        label="primary address v4",
        required=False,
        conflict=Conflict.CONSEQUENTIAL,
        help_text=(
            "Optional IPv4 CIDR to use as primary_ip4. It must match an address assigned to any "
            "interface on this device, either by this row or already in NetBox"
        ),
        example="192.0.2.10/24",
    ),
    ColumnSpec(
        key="primary_address_v6",
        label="primary address v6",
        required=False,
        conflict=Conflict.CONSEQUENTIAL,
        help_text=(
            "Optional IPv6 CIDR to use as primary_ip6. It must match an address assigned to any "
            "interface on this device, either by this row or already in NetBox"
        ),
        example="2001:db8:1::10/64",
    ),
)


# --- Sheet 3: cabling ----------------------------------------------------------------------
# A cable's identity is its pair of terminations, so both ends and both ports are IDENTITY: a
# different port is a different cable, not a conflicting one.
CABLING_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        key="a_end",
        label="a_end",
        required=True,
        conflict=Conflict.IDENTITY,
        help_text="Name of an existing device or patch panel",
        example="exa1-dh1-r01-leaf01",
    ),
    ColumnSpec(
        key="a_end_kind",
        label="a_end_kind",
        # Optional so that the documented default is reachable: a required column's blank cell is
        # rejected in S2, before any default could apply (Requirement 9.1).
        required=False,
        conflict=Conflict.IDENTITY,
        help_text="Selects how the port name is resolved. Defaults to device when blank",
        choices=("device", "panel"),
        example="device",
    ),
    ColumnSpec(
        key="a_port",
        label="a_port",
        required=True,
        conflict=Conflict.IDENTITY,
        help_text="Port name on the A end: an interface, or a panel's front/rear port",
        example="swp1",
    ),
    ColumnSpec(
        key="a_port_side",
        label="a_port_side",
        required=False,
        conflict=Conflict.IDENTITY,
        help_text="Panels only, and ignored for devices. Defaults to front",
        choices=("front", "rear"),
        example="",
    ),
    ColumnSpec(
        key="a_parent",
        label="a_parent",
        required=False,
        conflict=Conflict.IDENTITY,
        help_text=PARENT_HELP,
        example="",
    ),
    ColumnSpec(
        key="a_interface_type",
        label="a_interface_type",
        # DESCRIPTIVE is declarative here, the way `cable_profile`'s CONSEQUENTIAL is: `Conflict` is
        # required on every spec (Req 11.5c) and there is no value for "not comparable". The column
        # is read only when the interface does *not* exist, and where it does, Requirement 9.27's
        # INFO is an explicit check rather than the generic compare-to-existing path (D12).
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text=INTERFACE_TYPE_HELP,
        choices=INTERFACE_TYPES,
        example="",
    ),
    ColumnSpec(
        key="b_end",
        label="b_end",
        required=True,
        conflict=Conflict.IDENTITY,
        help_text="Name of an existing device or patch panel",
        example="exa1-dh1-r01-pp01",
    ),
    ColumnSpec(
        key="b_end_kind",
        label="b_end_kind",
        required=False,
        conflict=Conflict.IDENTITY,
        help_text="Selects how the port name is resolved. Defaults to device when blank",
        choices=("device", "panel"),
        example="panel",
    ),
    ColumnSpec(
        key="b_port",
        label="b_port",
        required=True,
        conflict=Conflict.IDENTITY,
        help_text="Port name on the B end: an interface, or a panel's front/rear port",
        example="MPO1",
    ),
    ColumnSpec(
        key="b_port_side",
        label="b_port_side",
        required=False,
        conflict=Conflict.IDENTITY,
        help_text="Panels only, and ignored for devices. Defaults to front",
        choices=("front", "rear"),
        example="rear",
    ),
    ColumnSpec(
        key="b_parent",
        label="b_parent",
        required=False,
        conflict=Conflict.IDENTITY,
        help_text=PARENT_HELP,
        example="",
    ),
    ColumnSpec(
        key="b_interface_type",
        label="b_interface_type",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text=INTERFACE_TYPE_HELP,
        choices=INTERFACE_TYPES,
        example="",
    ),
    ColumnSpec(
        key="cable_type",
        label="cable_type",
        required=True,
        conflict=Conflict.CONSEQUENTIAL,
        help_text="Validated against the cable-type choices NetBox reports",
        choices=("cat6", "cat6a", "mmf-om3", "mmf-om4", "smf-os2", "dac-passive", "aoc"),
        example="smf-os2",
    ),
    ColumnSpec(
        key="cable_profile",
        label="cable_profile",
        # CONSEQUENTIAL, alongside cable_type: the profile is what NetBox traces lanes through, so
        # an existing cable profiled differently from the sheet means the operator's picture of that
        # link is wrong, not merely differently labelled (D8, D10).
        required=False,
        conflict=Conflict.CONSEQUENTIAL,
        help_text=(
            "How the cable is built inside, for a breakout or trunk. Blank leaves the cable "
            "unprofiled, which is what every cable without one looks like today"
        ),
        choices=CABLE_PROFILES,
        example="",
    ),
    ColumnSpec(
        key="cable_group",
        label="cable_group",
        # IDENTITY like `a_end_kind` and `*_port_side`: it is this tool's vocabulary for reading the
        # sheet and is never sent to NetBox, so it cannot disagree with an existing object.
        required=False,
        conflict=Conflict.IDENTITY,
        help_text=(
            "Rows sharing a tag are one cable with several terminations, which is how a breakout "
            "is written. Blank means this row is a cable on its own"
        ),
        example="",
    ),
    ColumnSpec(
        key="cable_status",
        label="cable_status",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Defaults to connected",
        choices=("connected", "planned", "decommissioning"),
        example="connected",
    ),
    ColumnSpec(
        key="label",
        label="label",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Free text cable label",
        example="LNK-0001",
    ),
    ColumnSpec(
        key="length",
        label="length",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Number. Requires length_unit when supplied",
        example="3",
        numeric=True,
    ),
    ColumnSpec(
        key="length_unit",
        label="length_unit",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Validated against NetBox's choices when length is supplied",
        choices=("km", "m", "cm", "mi", "ft", "in"),
        example="m",
    ),
    ColumnSpec(
        key="color",
        label="color",
        required=False,
        conflict=Conflict.DESCRIPTIVE,
        help_text="Six-digit hex without the leading hash, e.g. 2196f3",
        example="",
    ),
)


SHEETS: dict[str, tuple[ColumnSpec, ...]] = {
    "hierarchy": HIERARCHY_COLUMNS,
    "devices": DEVICE_COLUMNS,
    "cabling": CABLING_COLUMNS,
}

# Worksheet name written into a generated xlsx, and the sheet looked for on upload (Req 5.3).
SHEET_TITLES: dict[str, str] = {
    "hierarchy": "Hierarchy",
    "devices": "Devices",
    "cabling": "Cabling",
}


def columns_for(importer: str) -> tuple[ColumnSpec, ...]:
    try:
        return SHEETS[importer]
    except KeyError:
        raise ValueError(
            f"unknown importer {importer!r}; expected one of {sorted(SHEETS)}"
        ) from None


def required_keys(importer: str) -> tuple[str, ...]:
    return tuple(spec.key for spec in columns_for(importer) if spec.required)


def spec_by_key(importer: str) -> dict[str, ColumnSpec]:
    return {spec.key: spec for spec in columns_for(importer)}


def example_row(importer: str) -> dict[str, str]:
    """The synthetic example written into templates, and refused if uploaded unchanged (Req 4.5)."""
    return {spec.key: spec.example for spec in columns_for(importer)}
