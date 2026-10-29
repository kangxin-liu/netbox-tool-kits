# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Connection status and per-importer readiness, both strictly read-only.

A brand-new NetBox has no device roles, manufacturers, device types, or patch panels, and iteration
1 creates none of them. On such an instance the device and cabling importers have nothing to resolve
against, which is expected behaviour — the tool's job is to say so up front rather than to look
broken after every row fails (Requirement 17).

Nothing in this module writes: it issues GET and OPTIONS only, and creates nothing (Req 17.4).
"""

import logging
from dataclasses import dataclass, field

from django.conf import settings

from importer.breakout_tags import BREAKOUT_TAG_FAMILY, TAGS_ENDPOINT
from importer.services.netbox_client import Failure, NetBoxClient, NetBoxUnavailable

logger = logging.getLogger("importer.netbox")

HIERARCHY_PREREQUISITES = (
    "Each row needs an existing site and parent location. The importer creates only their direct "
    "child location and rack."
)


@dataclass(frozen=True)
class Fact:
    """One line of the readiness summary."""

    label: str
    value: str
    level: str = "ok"  # ok | warn | error
    detail: str = ""


@dataclass(frozen=True)
class ConnectionStatus:
    """Shown in the header on every page (Requirements 2.4, 2.5)."""

    reachable: bool
    host: str
    version: str = ""
    version_supported: bool = True
    category: Failure | None = None
    detail: str = ""

    @property
    def can_import(self) -> bool:
        """False disables the apply action (Requirements 2.5, 2.7)."""
        return self.reachable and self.version_supported

    @property
    def label(self) -> str:
        if not self.reachable:
            return f"unreachable ({self.category.value if self.category else 'unknown'})"
        if not self.version_supported:
            return f"NetBox {self.version} — unsupported"
        return f"NetBox {self.version}"


@dataclass(frozen=True)
class Readiness:
    """The per-importer prerequisite summary (Requirement 17.2)."""

    importer: str
    ready: bool
    facts: list[Fact] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    note: str = ""


def connection_status(client: NetBoxClient) -> ConnectionStatus:
    """Probe the target once and describe it, never raising into a view."""
    try:
        status = client.status()
    except NetBoxUnavailable as unavailable:
        logger.warning(
            "netbox unreachable host=%s category=%s", unavailable.host, unavailable.category.value
        )
        return ConnectionStatus(
            reachable=False,
            host=unavailable.host,
            category=unavailable.category,
            detail=unavailable.operator_message,
        )
    return ConnectionStatus(
        reachable=True,
        host=status.host,
        version=status.version,
        version_supported=status.supported,
        detail=status.reason,
    )


def readiness_for(importer: str, client: NetBoxClient) -> Readiness:
    """Count what the importer depends on. Read-only; creates nothing (Requirement 17.4)."""
    try:
        if importer == "hierarchy":
            return _hierarchy_readiness(client)
        if importer == "devices":
            return _devices_readiness(client)
        if importer == "cabling":
            return _cabling_readiness(client)
    except NetBoxUnavailable as unavailable:
        return Readiness(
            importer=importer,
            ready=False,
            blockers=[unavailable.operator_message],
        )

    raise ValueError(f"unknown importer {importer!r}")


def _hierarchy_readiness(client: NetBoxClient) -> Readiness:
    sites = client.count("dcim.sites")
    locations = client.count("dcim.locations")
    facts = [
        Fact(
            label="Sites defined",
            value=str(sites),
            level="ok" if sites else "error",
            detail="" if sites else "Create the target site in NetBox first.",
        ),
        Fact(
            label="Locations defined",
            value=str(locations),
            level="ok" if locations else "error",
            detail="" if locations else "Create the parent location in NetBox first.",
        ),
    ]
    blockers: list[str] = []
    if not sites:
        blockers.append("NetBox holds no sites, and the hierarchy importer never creates them.")
    if not locations:
        blockers.append(
            "NetBox holds no locations. Create each parent location before importing its children."
        )
    return Readiness(
        importer="hierarchy",
        ready=not blockers,
        facts=facts,
        blockers=blockers,
        note=HIERARCHY_PREREQUISITES,
    )


def _devices_readiness(client: NetBoxClient) -> Readiness:
    facts: list[Fact] = []
    blockers: list[str] = []

    device_types = client.count("dcim.device-types")
    facts.append(
        Fact(
            label="Device types defined",
            value=str(device_types),
            level="ok" if device_types else "error",
            detail=(
                ""
                if device_types
                else "Import device types into NetBox before uploading a device sheet."
            ),
        )
    )
    if not device_types:
        blockers.append(
            "NetBox holds no device types, so no device row can resolve its model. Import the "
            "device types you use before uploading this sheet."
        )

    role_slug = settings.NETBOX_DEFAULT_DEVICE_ROLE
    if not role_slug:
        facts.append(
            Fact(
                label="Default device role",
                value="not configured",
                level="warn",
                detail=(
                    "NETBOX_DEFAULT_DEVICE_ROLE is unset, so every row must name its own "
                    "device_role."
                ),
            )
        )
    else:
        # Requirement 17.3: warn at page load, not only at validation time.
        matches = client.count("dcim.device-roles", slug=role_slug)
        resolved = matches == 1
        facts.append(
            Fact(
                label="Default device role",
                value=role_slug if resolved else f"{role_slug} — not found",
                level="ok" if resolved else "warn",
                detail=(
                    ""
                    if resolved
                    else (
                        f"NETBOX_DEFAULT_DEVICE_ROLE is {role_slug!r}, which does not exist in "
                        f"NetBox. Rows without their own device_role will be rejected."
                    )
                ),
            )
        )

    roles = client.count("dcim.device-roles")
    facts.append(
        Fact(
            label="Device roles defined",
            value=str(roles),
            level="ok" if roles else "error",
            detail="" if roles else "NetBox requires a role on every device.",
        )
    )
    if not roles:
        blockers.append(
            "NetBox holds no device roles. Every device needs one, and iteration 1 does not create "
            "them, so create the roles you use first."
        )

    mgmt = client.count("dcim.interface-templates", mgmt_only=True)
    loopback = client.count("dcim.interface-templates", type="loopback")
    facts.append(
        Fact(
            label="Device types with a management interface",
            value=str(mgmt),
            level="ok" if mgmt else "warn",
            detail=(
                ""
                if mgmt
                else (
                    "No interface template is marked mgmt_only. A device sheet with a management "
                    "address will not resolve until each network device type has exactly one."
                )
            ),
        )
    )
    facts.append(
        Fact(
            label="Device types with a loopback interface",
            value=str(loopback),
            level="ok" if loopback else "warn",
            detail=(
                ""
                if loopback
                else (
                    "No interface template is typed loopback. A device sheet with a loopback "
                    "address will not resolve until each network device type has exactly one."
                )
            ),
        )
    )

    return Readiness(importer="devices", ready=not blockers, facts=facts, blockers=blockers)


def _cabling_readiness(client: NetBoxClient) -> Readiness:
    facts: list[Fact] = []
    blockers: list[str] = []

    devices = client.count("dcim.devices")
    facts.append(
        Fact(
            label="Devices in NetBox",
            value=str(devices),
            level="ok" if devices else "error",
            detail="" if devices else "Cabling terminates on devices that already exist.",
        )
    )
    if not devices:
        blockers.append(
            "NetBox holds no devices, so no cable end can resolve. Run the device sheet first."
        )

    interfaces = client.count("dcim.interfaces")
    facts.append(
        Fact(
            label="Interfaces available",
            value=str(interfaces),
            level="ok" if interfaces else "warn",
            detail=(
                ""
                if interfaces
                else (
                    "No interfaces exist. A device type with no interface templates produces a "
                    "device with no ports."
                )
            ),
        )
    )

    front = client.count("dcim.front-ports")
    rear = client.count("dcim.rear-ports")
    panel_ports = front + rear
    facts.append(
        Fact(
            label="Patch-panel ports (front / rear)",
            value=f"{front} / {rear}",
            level="ok" if panel_ports else "warn",
            detail=(
                ""
                if panel_ports
                else (
                    "No front or rear ports exist, so panel rows cannot resolve. Panels must be "
                    "built in NetBox first — this importer does not create them."
                )
            ),
        )
    )

    # Requirement 17.2: the flag changes what an unknown port name *does*, so an operator reading
    # this panel before uploading should not have to guess which behaviour they are about to get.
    autocreate = settings.NETBOX_AUTOCREATE_INTERFACES
    facts.append(
        Fact(
            label="Create missing interfaces",
            value="on" if autocreate else "off",
            level="warn" if autocreate else "ok",
            detail=(
                "A device port this sheet names and NetBox does not have will be created — from "
                "the device type's template where there is one, otherwise from a_interface_type / "
                "b_interface_type. Each one is a warning to acknowledge before applying."
                if autocreate
                else (
                    "A port name that does not resolve blocks the file. Set "
                    "NETBOX_AUTOCREATE_INTERFACES to let a sheet create the interfaces its device "
                    "types define."
                )
            ),
        )
    )

    tag_records = client.get_all(TAGS_ENDPOINT)
    held_tag_slugs = {str(record.get("slug") or "").strip().casefold() for record in tag_records}
    missing_tags = sorted(BREAKOUT_TAG_FAMILY - held_tag_slugs)
    facts.append(
        Fact(
            label="Breakout parent tags",
            value=(
                "all present"
                if not missing_tags
                else f"{len(BREAKOUT_TAG_FAMILY) - len(missing_tags)} / "
                f"{len(BREAKOUT_TAG_FAMILY)} present"
            ),
            level="ok" if not missing_tags else "warn",
            detail=(
                ""
                if not missing_tags
                else (
                    "Create these tag slugs before importing a parented cable profile that "
                    f"requires them: {', '.join(missing_tags)}."
                )
            ),
        )
    )

    if devices and not interfaces and not panel_ports:
        blockers.append(
            "The devices in NetBox have no ports at all, so nothing can be cabled. Check that the "
            "device types define interface, front-port, and rear-port templates."
        )

    return Readiness(importer="cabling", ready=not blockers, facts=facts, blockers=blockers)
