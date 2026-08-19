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

# The template's guidance line sits on line 2 and starts with this marker so the parser can drop it
# without treating it as data (Requirements 4.7, 5.11).
GUIDANCE_MARKER = "# guidance:"


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
    """snake_case, and the exact header text a template is generated with."""

    label: str
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
# Location → dcim.Region, Datacenter → dcim.Site, Datahall → dcim.Location (D1). All three are
# identity columns: they are how an existing object is found, so they cannot conflict.
HIERARCHY_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        key="location",
        label="location",
        required=True,
        conflict=Conflict.IDENTITY,
        help_text="Geographic grouping, created as a NetBox region. A slug is derived from it",
        example="Example Region",
    ),
    ColumnSpec(
        key="datacenter",
        label="datacenter",
        required=True,
        conflict=Conflict.IDENTITY,
        help_text="The facility, created as a NetBox site within the row's location",
        example="EXA1",
    ),
    ColumnSpec(
        key="datahall",
        label="datahall",
        required=True,
        conflict=Conflict.IDENTITY,
        help_text="Room, created as a NetBox location inside the site. Scoped to its datacenter",
        example="DH1",
    ),
    ColumnSpec(
        key="rack",
        label="rack",
        required=True,
        conflict=Conflict.IDENTITY,
        help_text="Must read <datacenter>-<datahall>-<rack> and agree with those two columns",
        example="EXA1-DH1-R01",
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
        key="cable_type",
        label="cable_type",
        required=True,
        conflict=Conflict.CONSEQUENTIAL,
        help_text="Validated against the cable-type choices NetBox reports",
        choices=("cat6", "cat6a", "mmf-om3", "mmf-om4", "smf-os2", "dac-passive", "aoc"),
        example="smf-os2",
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
