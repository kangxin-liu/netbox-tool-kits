# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Sheet 2 — one row per device, mounted into a rack that already exists (Requirement 8).

Almost everything a device row refers to is a prerequisite rather than something to create: the
rack, the device role, and the tenant must already be in NetBox, and a row naming one that is not
there is rejected saying so (Requirements 8.1, 8.8, 17.1). The single exception is the device type,
which `NETBOX_AUTOCREATE_DEVICE_TYPES` may create together with its manufacturer — off by default,
because a typo would otherwise leave junk device types behind in a shared instance (decision D4).

Rack units are the part worth being careful about. `bottom_ru` is the lowest unit the device
occupies and is what NetBox calls `position`, so a row claims `bottom_ru` through
`bottom_ru + ru_height - 1` on one face. That claim is checked three ways: against the rack's own
height, against the other rows of the same file (S3), and against the devices already mounted on
that face (S4). The last of those is why every device in every rack the sheet touches is prefetched
rather than looked up row by row.

`ru_height` is never submitted. NetBox takes a device's height from its device type, so the column
exists to be cross-checked against that type (Requirement 8.6) — a row that disagrees with it would
draw a rack elevation the operator is not expecting — and to size a device type that autocreate
creates.

A device that already exists is not recreated (Requirements 8.10, 11.2, 11.3). Remaining work
on that row — loopback and management addresses, then a field-scoped PATCH of the primary IP —
is still planned (Requirements 8.20, 8.19). A row whose device *and* addresses already match is
skipped in full. Device-field disagreements still block; they are never patched.
"""

from dataclasses import dataclass, field
from typing import Any, ClassVar

from django.conf import settings

from importer.device_addresses import (
    ADDRESS_COLUMNS,
    ParsedAddress,
    canonical,
    choice_value,
    displayed_primary,
    parse_device_ip,
    parse_row_addresses,
    primary_columns,
    related_id,
    resolve_roles,
)
from importer.importers.base import (
    BaseImporter,
    CreateAction,
    PlannedRow,
    Ref,
    Registry,
    RowOutcome,
)
from importer.naming import derive_slug, parse_rack_name
from importer.services.lookups import CHUNK_SIZE, Lookups, fold
from importer.services.sheets import Row
from importer.validation import (
    Code,
    IssueCollector,
    RowError,
    Severity,
    compare_to_existing,
    netbox_text,
)

MANUFACTURERS = "dcim.manufacturers"
DEVICE_TYPES = "dcim.device-types"
DEVICES = "dcim.devices"
RACKS = "dcim.racks"
ROLES = "dcim.device-roles"
TENANTS = "tenancy.tenants"
INTERFACE_TEMPLATES = "dcim.interface-templates"
INTERFACES = "dcim.interfaces"
IP_ADDRESSES = "ipam.ip-addresses"

DEFAULT_STATUS = "active"

NOT_FOUND = {
    "management": Code.MGMT_INTERFACE_NOT_FOUND,
    "loopback": Code.LOOPBACK_INTERFACE_NOT_FOUND,
}
AMBIGUOUS = {
    "management": Code.MGMT_INTERFACE_AMBIGUOUS,
    "loopback": Code.LOOPBACK_INTERFACE_AMBIGUOUS,
}


def units(bottom: int, height: int) -> tuple[int, int]:
    """The lowest and highest unit a device of `height` occupies from `bottom` (Requirement 8.3)."""
    return bottom, bottom + height - 1


def overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return first[0] <= second[1] and second[0] <= first[1]


def span(occupied: tuple[int, int]) -> str:
    bottom, top = occupied
    return f"U{bottom}" if bottom == top else f"U{bottom}-U{top}"


def model_of(device_type: dict[str, Any]) -> str:
    return str(device_type.get("model") or device_type.get("display") or "")


@dataclass
class TypeResolution:
    """What `model` resolved to: a device type that exists, or one this run will create."""

    key: tuple[Any, ...]
    existing: dict[str, Any] | None = None
    actions: list[CreateAction] = field(default_factory=list)

    @property
    def reference(self) -> Any:
        """Its id, or a `Ref` that apply resolves once the device-type batch has been posted."""
        if self.existing is not None:
            return int(self.existing["id"])
        return Ref(DEVICE_TYPES, self.key)


class DeviceImporter(BaseImporter):
    name = "devices"
    endpoint_order = (MANUFACTURERS, DEVICE_TYPES, DEVICES, INTERFACES, IP_ADDRESSES)
    apply_phases: ClassVar[tuple[tuple[str, str], ...]] = (
        (MANUFACTURERS, "post"),
        (DEVICE_TYPES, "post"),
        (DEVICES, "post"),
        (INTERFACES, "post"),
        (IP_ADDRESSES, "post"),
        (DEVICES, "patch"),
    )

    choice_sources: ClassVar[dict[str, tuple[str, str]]] = {
        "status": (DEVICES, "status"),
        "rack_face": (DEVICES, "face"),
    }

    def __init__(self) -> None:
        self._devices_by_rack: dict[int, list[dict[str, Any]]] = {}
        self._asset_tag_owners: dict[str, dict[str, Any]] = {}
        self._templates_by_type: dict[int, list[dict[str, Any]]] = {}

    # --- S2 ---------------------------------------------------------------------------------

    def extra_cell_checks(self, row: Row, lookups: Lookups) -> list[RowError]:
        # The rack name is the join key between all three sheets, so its shape is checked here as
        # well as in sheet 1 (Requirement 6.2) rather than surfacing as a lookup miss.
        errors: list[RowError] = []
        try:
            parse_rack_name(row.get("rack"))
        except RowError as error:
            errors.append(error)
        for column in ADDRESS_COLUMNS:
            raw = row.get(column)
            if not str(raw).strip():
                continue
            try:
                parse_device_ip(column, raw)
            except RowError as error:
                errors.append(error)
        return errors

    # --- S3 ---------------------------------------------------------------------------------

    def check_consistency(self, rows: list[Row], collector: IssueCollector) -> None:
        seen_devices: dict[str, int] = {}
        seen_tags: dict[str, tuple[str, int]] = {}
        claimed: dict[tuple[str, str], list[tuple[tuple[int, int], str, int]]] = {}
        seen_ips: dict[str, tuple[int, str]] = {}

        for row in rows:
            name = row.get("device_name").strip()
            self._check_duplicate_name(row, name, seen_devices, collector)
            self._check_duplicate_tag(row, name, seen_tags, collector)
            self._check_duplicate_ips(row, seen_ips, collector)

            occupied = self._occupied(row)
            if occupied is None:
                continue  # a malformed number was already reported in S2

            # Requirement 8.5: two devices can hold the same units in different racks, and on
            # opposite faces of one rack, so the claim is scoped to both.
            face = fold(row.get("rack_face"))
            taken = claimed.setdefault((fold(row.get("rack")), face), [])
            clash = next((item for item in taken if overlap(occupied, item[0])), None)
            if clash is None:
                taken.append((occupied, name, row.line))
                continue

            other_units, other_name, other_line = clash
            collector.add(
                Severity.ERROR,
                Code.RU_RANGE_OVERLAP,
                line=row.line,
                column="bottom_ru",
                value=row.get("bottom_ru"),
                expected=f"a range not overlapping rack {row.get('rack')} {face} face",
                message=(
                    f"{name!r} would occupy {span(occupied)} on the {face} face of rack "
                    f"{row.get('rack')!r}, which line {other_line} already claims for "
                    f"{other_name!r} ({span(other_units)}). Move one of them."
                ),
                related_line=other_line,
            )

    def _check_duplicate_name(
        self, row: Row, name: str, seen: dict[str, int], collector: IssueCollector
    ) -> None:
        """Requirement 8.11: the later row is the one rejected, and it names the earlier line."""
        folded = fold(name)
        if not folded:
            return
        if folded not in seen:
            seen[folded] = row.line
            return
        collector.add(
            Severity.ERROR,
            Code.DUPLICATE_DEVICE_IN_FILE,
            line=row.line,
            column="device_name",
            value=name,
            expected="one row per device",
            message=(
                f"Device {name!r} is already declared on line {seen[folded]}. Remove the "
                f"duplicate row."
            ),
            related_line=seen[folded],
        )

    def _check_duplicate_tag(
        self,
        row: Row,
        name: str,
        seen: dict[str, tuple[str, int]],
        collector: IssueCollector,
    ) -> None:
        """An asset tag is unique across NetBox, so two rows cannot both claim one."""
        tag = row.get("asset_tag").strip()
        if not tag:
            return
        owner = seen.get(fold(tag))
        if owner is None:
            seen[fold(tag)] = (name, row.line)
            return
        collector.add(
            Severity.ERROR,
            Code.DUPLICATE_ASSET_TAG_IN_FILE,
            line=row.line,
            column="asset_tag",
            value=tag,
            expected="an asset tag no other row uses",
            message=(
                f"Asset tag {tag!r} is already claimed by {owner[0]!r} on line {owner[1]}. "
                f"An asset tag is unique across NetBox, so only one device can carry it."
            ),
            related_line=owner[1],
        )

    def _check_duplicate_ips(
        self,
        row: Row,
        seen: dict[str, tuple[int, str]],
        collector: IssueCollector,
    ) -> None:
        """Requirement 8.22: the same address in two cells — two rows, or two columns."""
        for column in ADDRESS_COLUMNS:
            raw = row.get(column).strip()
            if not raw or "/" not in raw:
                continue
            try:
                key = canonical(raw)
            except ValueError:
                continue
            previous = seen.get(key)
            if previous is None:
                seen[key] = (row.line, column)
                continue
            other_line, other_column = previous
            collector.add(
                Severity.ERROR,
                Code.DUPLICATE_IP_IN_FILE,
                line=row.line,
                column=column,
                value=raw,
                expected="an address no other cell in this file uses",
                message=(
                    f"{raw!r} is already declared on line {other_line} ({other_column}). "
                    f"An address can only be assigned once."
                ),
                related_line=other_line,
            )

    @staticmethod
    def _occupied(row: Row) -> tuple[int, int] | None:
        try:
            return units(int(row.get("bottom_ru")), int(row.get("ru_height")))
        except ValueError:
            return None

    # --- S4 ---------------------------------------------------------------------------------

    def prefetch(self, rows: list[Row], lookups: Lookups) -> None:
        lookups.prefetch(RACKS, names=[row.get("rack") for row in rows])

        # A device type's natural key is `model`, not `name`, so the generic name filter would
        # narrow nothing and fetch the whole catalogue instead.
        models = sorted({row.get("model").strip() for row in rows if row.get("model").strip()})
        if models:
            lookups.prefetch(DEVICE_TYPES, model__ie=models)

        self._prefetch_roles(rows, lookups)
        self._prefetch_manufacturers(rows, lookups)

        tenants = [row.get("tenant") for row in rows if row.get("tenant")]
        if tenants:
            lookups.prefetch(TENANTS, names=tenants)

        lookups.prefetch(DEVICES, names=[row.get("device_name") for row in rows])
        rack_ids = sorted({int(rack["id"]) for rack in lookups.all_records(RACKS)})
        if rack_ids:
            # Everything already mounted in the racks this sheet touches, so the overlap check of
            # Requirement 8.5 costs no request per row (Requirement 15.2).
            lookups.prefetch(DEVICES, rack_id=rack_ids)
        self._index_devices(lookups)
        self._prefetch_addresses(rows, lookups)

    def _prefetch_addresses(self, rows: list[Row], lookups: Lookups) -> None:
        """Interface templates, existing device interfaces, and any IPAM row the sheet names."""
        type_ids = {int(record["id"]) for record in lookups.all_records(DEVICE_TYPES)}
        for device in lookups.all_records(DEVICES):
            type_id = related_id(device.get("device_type"))
            if type_id is not None:
                type_ids.add(type_id)
        if type_ids:
            lookups.prefetch(INTERFACE_TEMPLATES, device_type_id=sorted(type_ids))
        self._templates_by_type = {}
        for template in lookups.all_records(INTERFACE_TEMPLATES):
            type_id = related_id(template.get("device_type"))
            if type_id is None:
                continue
            self._templates_by_type.setdefault(type_id, []).append(template)

        device_ids = [int(device["id"]) for device in lookups.all_records(DEVICES)]
        if device_ids:
            lookups.prefetch_terminations(device_ids, ("interface",))
            lookups.prefetch(IP_ADDRESSES, device_id=device_ids)

        wanted: list[str] = []
        for row in rows:
            for column in ADDRESS_COLUMNS:
                raw = row.get(column).strip()
                if not raw or "/" not in raw:
                    continue
                try:
                    wanted.append(canonical(raw))
                except ValueError:
                    continue
        unique = sorted(set(wanted))
        for start in range(0, len(unique), CHUNK_SIZE):
            lookups.prefetch(IP_ADDRESSES, address=unique[start : start + CHUNK_SIZE])

    def _prefetch_roles(self, rows: list[Row], lookups: Lookups) -> None:
        supplied = [row.get("device_role") for row in rows if row.get("device_role")]
        if supplied:
            lookups.prefetch(ROLES, names=supplied)
        slugs = {derive_slug(name) for name in supplied}
        # The configured default is a slug and may name a role no row mentions (decision D2).
        if settings.NETBOX_DEFAULT_DEVICE_ROLE:
            slugs.add(settings.NETBOX_DEFAULT_DEVICE_ROLE.strip())
        slugs.discard("")
        if slugs:
            lookups.prefetch(ROLES, slug__ie=sorted(slugs))

    def _prefetch_manufacturers(self, rows: list[Row], lookups: Lookups) -> None:
        if not settings.NETBOX_AUTOCREATE_DEVICE_TYPES:
            # Nothing else reads the column, so with autocreate off this is a request not worth
            # issuing (Requirement 8.7).
            return
        names = [row.get("manufacturer") for row in rows if row.get("manufacturer")]
        if not names:
            return
        lookups.prefetch(MANUFACTURERS, names=names)
        slugs = sorted({derive_slug(name) for name in names} - {""})
        if slugs:
            lookups.prefetch(MANUFACTURERS, slug__ie=slugs)

    def _index_devices(self, lookups: Lookups) -> None:
        """Group the prefetched devices the two ways S4 reads them, at no further request cost."""
        self._devices_by_rack = {}
        self._asset_tag_owners = {}
        for record in lookups.all_records(DEVICES):
            rack = record.get("rack")
            rack_id = rack.get("id") if isinstance(rack, dict) else rack
            if rack_id is not None:
                self._devices_by_rack.setdefault(int(rack_id), []).append(record)
            tag = record.get("asset_tag")
            if tag:
                self._asset_tag_owners.setdefault(fold(tag), record)

    def plan_row(
        self,
        row: Row,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
    ) -> PlannedRow:
        planned = PlannedRow(line=row.line)
        name = row.get("device_name").strip()

        existing = lookups.get_or_none(DEVICES, name=name)
        if existing is not None:
            self._already_present(row, existing, lookups, registry, state, collector, planned)
            return planned

        rack = self._rack(row, lookups, registry, state, collector, planned)
        if rack is not None:
            self._check_fits(row, name, rack, lookups, collector, planned)
            self._check_free(row, name, rack, lookups, collector, planned)
        device_type = self._device_type(row, lookups, registry, state, collector, planned)
        role_id = self._role(row, lookups, registry, state, collector, planned)
        tenant_id = self._tenant(row, lookups, registry, state, collector, planned)
        self._check_asset_tag(row, None, lookups, collector, planned)

        unresolved = rack is None or device_type is None or role_id is None
        if unresolved or planned.outcome == RowOutcome.BLOCKED:
            planned.outcome = RowOutcome.BLOCKED
            return planned

        planned.actions.extend(device_type.actions)
        planned.actions.append(
            self._device_action(row, name, rack, device_type, role_id, tenant_id)
        )
        planned.resolved["device_name"] = f"{name} (create)"
        self._plan_addresses(
            row,
            lookups,
            registry,
            state,
            collector,
            planned,
            device_name=name,
            existing=None,
            device_type=device_type.existing,
            device_ref=Ref(DEVICES, Registry.key(name)),
        )
        return planned

    # --- one reference at a time ------------------------------------------------------------

    def _register(
        self,
        state: dict[str, str],
        registry: Registry,
        endpoint: str,
        key: tuple[Any, ...],
        existing: dict[str, Any],
        signature: str,
    ) -> None:
        """Record an existing object's id and the state the plan depends on (Requirement 10.14)."""
        registry.remember(endpoint, key, int(existing["id"]))
        state[f"{endpoint}:{'/'.join(str(part) for part in key)}"] = signature

    def _rack(
        self,
        row: Row,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> dict[str, Any] | None:
        """Requirement 8.1: exactly one existing rack, and this importer never creates one."""
        try:
            rack_name = parse_rack_name(row.get("rack"))
        except RowError as error:
            collector.add_row_error(error, row.line)
            planned.outcome = RowOutcome.BLOCKED
            return None

        existing = lookups.get_or_none(RACKS, name=rack_name.raw)
        if existing is None:
            collector.add(
                Severity.ERROR,
                Code.RACK_NOT_FOUND,
                line=row.line,
                column="rack",
                value=rack_name.raw,
                expected="the name of a rack that already exists in NetBox",
                message=(
                    f"No rack named {rack_name.raw!r} exists in NetBox. This importer never "
                    f"creates racks: run the hierarchy sheet first, or correct the name."
                ),
            )
            planned.outcome = RowOutcome.BLOCKED
            return None

        self._register(
            state,
            registry,
            RACKS,
            Registry.key(existing.get("name")),
            existing,
            f"id={existing['id']},u_height={existing.get('u_height')},"
            f"site={(existing.get('site') or {}).get('id')}",
        )
        planned.resolved["rack"] = f"{existing.get('name')} (existing)"
        return existing

    def _check_fits(
        self,
        row: Row,
        name: str,
        rack: dict[str, Any],
        lookups: Lookups,
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> None:
        """Requirement 8.4: the top unit must be within the rack, and the message says how tall."""
        occupied = self._occupied(row)
        height = rack.get("u_height")
        if occupied is None or height is None:
            return
        if occupied[1] <= int(float(height)):
            return
        collector.add(
            Severity.ERROR,
            Code.RU_RANGE_EXCEEDS_RACK,
            line=row.line,
            column="bottom_ru",
            value=row.get("bottom_ru"),
            expected=f"a range ending at U{int(float(height))} or below",
            message=(
                f"{name!r} would occupy {span(occupied)}, but rack {rack.get('name')!r} is "
                f"{int(float(height))}U high, so its top unit is U{int(float(height))}."
            ),
            related_object=f"Rack {rack.get('name')}",
            related_url=self._url(lookups, RACKS, rack),
        )
        planned.outcome = RowOutcome.BLOCKED

    def _check_free(
        self,
        row: Row,
        name: str,
        rack: dict[str, Any],
        lookups: Lookups,
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> None:
        """Requirement 8.5: the units must be free on that face of that rack."""
        occupied = self._occupied(row)
        if occupied is None:
            return
        face = fold(row.get("rack_face"))

        for other in self._devices_by_rack.get(int(rack["id"]), []):
            position = other.get("position")
            other_height = (other.get("device_type") or {}).get("u_height")
            if position is None or other_height is None:
                continue  # not mounted at a known position, so it claims nothing
            if fold(netbox_text(other.get("face"))) != face:
                continue
            theirs = units(int(float(position)), int(float(other_height)))
            if not overlap(occupied, theirs):
                continue
            collector.add(
                Severity.ERROR,
                Code.RU_RANGE_OVERLAP,
                line=row.line,
                column="bottom_ru",
                value=row.get("bottom_ru"),
                expected=f"a range not overlapping rack {rack.get('name')} {face} face",
                message=(
                    f"{name!r} would occupy {span(occupied)} on the {face} face of rack "
                    f"{rack.get('name')!r}, which {other.get('name')!r} already holds "
                    f"({span(theirs)})."
                ),
                related_object=f"Device {other.get('name')}",
                related_url=self._url(lookups, DEVICES, other),
            )
            planned.outcome = RowOutcome.BLOCKED
            return

    def _device_type(
        self,
        row: Row,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> TypeResolution | None:
        model = row.get("model").strip()
        resolution = TypeResolution(key=Registry.key(model))

        existing = lookups.get_or_none(DEVICE_TYPES, model=model)
        if existing is not None:
            resolution.existing = existing
            self._check_type_height(row, existing, lookups, collector, planned)
            self._register(
                state,
                registry,
                DEVICE_TYPES,
                resolution.key,
                existing,
                f"id={existing['id']},u_height={existing.get('u_height')}",
            )
            planned.resolved["model"] = f"{existing.get('model')} (existing)"
            return resolution

        # Requirement 8.7: unknown models are rejected unless the operator has opted in.
        if not settings.NETBOX_AUTOCREATE_DEVICE_TYPES:
            collector.add(
                Severity.ERROR,
                Code.DEVICE_TYPE_NOT_FOUND,
                line=row.line,
                column="model",
                value=model,
                expected="the model of a device type that already exists in NetBox",
                message=(
                    f"No device type with model {model!r} exists in NetBox. Import it before "
                    f"running this sheet, or enable NETBOX_AUTOCREATE_DEVICE_TYPES and give the "
                    f"row a manufacturer."
                ),
            )
            planned.outcome = RowOutcome.BLOCKED
            return None

        return self._autocreate_type(
            row, model, resolution, lookups, registry, state, collector, planned
        )

    def _check_type_height(
        self,
        row: Row,
        device_type: dict[str, Any],
        lookups: Lookups,
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> None:
        """Requirement 8.6: a rack elevation is drawn from the type's height, not the sheet's."""
        held = device_type.get("u_height")
        if held is None:
            return
        try:
            wanted = int(row.get("ru_height"))
        except ValueError:
            return
        if float(held) == float(wanted):
            return
        collector.add(
            Severity.ERROR,
            Code.RU_HEIGHT_DISAGREES_WITH_TYPE,
            line=row.line,
            column="ru_height",
            value=str(wanted),
            expected=netbox_text(held),
            message=(
                f"This row says {model_of(device_type)!r} is {wanted}U, but the device type in "
                f"NetBox is {netbox_text(held)}U. NetBox draws the rack elevation from the device "
                f"type, so one of the two is wrong."
            ),
            related_object=f"Device type {model_of(device_type)}",
            related_url=self._url(lookups, DEVICE_TYPES, device_type),
        )
        planned.outcome = RowOutcome.BLOCKED

    def _autocreate_type(
        self,
        row: Row,
        model: str,
        resolution: TypeResolution,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> TypeResolution | None:
        """Requirement 8.7 with the flag on: create the type, and make the operator look at it."""
        manufacturer = row.get("manufacturer").strip()
        if not manufacturer:
            collector.add(
                Severity.ERROR,
                Code.MANUFACTURER_REQUIRED_FOR_AUTOCREATE,
                line=row.line,
                column="manufacturer",
                value="(blank)",
                expected="the manufacturer of the device type to create",
                message=(
                    f"No device type with model {model!r} exists in NetBox. "
                    f"NETBOX_AUTOCREATE_DEVICE_TYPES is on, but NetBox requires a manufacturer on "
                    f"every device type and this row leaves the column blank."
                ),
            )
            planned.outcome = RowOutcome.BLOCKED
            return None

        for column, value in (("manufacturer", manufacturer), ("model", model)):
            if derive_slug(value):
                continue
            collector.add(
                Severity.ERROR,
                Code.SLUG_NOT_DERIVABLE,
                line=row.line,
                column=column,
                value=value,
                expected="a name containing at least one letter or digit",
                message=(
                    f"No URL-safe slug can be derived from {value!r}, and NetBox requires one on "
                    f"a device type it is asked to create."
                ),
            )
            planned.outcome = RowOutcome.BLOCKED
        if planned.outcome == RowOutcome.BLOCKED:
            return None

        maker = self._manufacturer(row, manufacturer, resolution, lookups, registry, state, planned)
        height = int(row.get("ru_height"))
        resolution.actions.append(
            CreateAction(
                endpoint=DEVICE_TYPES,
                payload={
                    "model": model,
                    "slug": derive_slug(model),
                    "manufacturer": maker,
                    # The sheet is the only height on offer, which is exactly why the row has to
                    # be reviewed before it is applied.
                    "u_height": height,
                },
                key=resolution.key,
                label=f"Device type {model}",
                line=row.line,
                object_kind="device type",
            )
        )
        made = "" if isinstance(maker, int) else f" and the manufacturer {manufacturer!r}"
        collector.add(
            Severity.WARNING,
            Code.DEVICE_TYPE_WILL_BE_CREATED,
            line=row.line,
            column="model",
            value=model,
            expected="a device type that already exists, or one you are happy to create",
            message=(
                f"No device type with model {model!r} exists in NetBox, so this run will create "
                f"it{made} at {height}U from this row. Check the model spelling before applying: "
                f"a typo becomes a device type nobody wanted."
            ),
        )
        planned.resolved["model"] = f"{model} (create)"
        return resolution

    def _manufacturer(
        self,
        row: Row,
        name: str,
        resolution: TypeResolution,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        planned: PlannedRow,
    ) -> Any:
        """The manufacturer's id, or a `Ref` to one this run creates first."""
        slug = derive_slug(name)
        existing = lookups.get_or_none(MANUFACTURERS, name=name) or lookups.get_or_none(
            MANUFACTURERS, slug=slug
        )
        if existing is not None:
            self._register(
                state, registry, MANUFACTURERS, Registry.key(slug), existing, f"id={existing['id']}"
            )
            planned.resolved["manufacturer"] = f"{existing.get('name')} (existing)"
            return int(existing["id"])

        resolution.actions.append(
            CreateAction(
                endpoint=MANUFACTURERS,
                payload={"name": name, "slug": slug},
                key=Registry.key(slug),
                label=f"Manufacturer {name}",
                line=row.line,
                object_kind="manufacturer",
            )
        )
        planned.resolved["manufacturer"] = f"{name} (create)"
        return Ref(MANUFACTURERS, Registry.key(slug))

    def _role(
        self,
        row: Row,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> int | None:
        """Requirement 8.8 and decision D2: the column, then the configured default, then reject."""
        supplied = row.get("device_role").strip()
        default = (settings.NETBOX_DEFAULT_DEVICE_ROLE or "").strip()
        wanted = supplied or default

        if not wanted:
            collector.add(
                Severity.ERROR,
                Code.DEVICE_ROLE_NOT_FOUND,
                line=row.line,
                column="device_role",
                value="(blank)",
                expected="a device_role, or NETBOX_DEFAULT_DEVICE_ROLE set to an existing role",
                message=(
                    "NetBox requires a role on every device. This row names none and "
                    "NETBOX_DEFAULT_DEVICE_ROLE is not configured, so there is nothing to fall "
                    "back to."
                ),
            )
            planned.outcome = RowOutcome.BLOCKED
            return None

        role = lookups.get_or_none(ROLES, name=wanted) or lookups.get_or_none(
            ROLES, slug=derive_slug(wanted)
        )
        if role is None:
            source = "this row" if supplied else "NETBOX_DEFAULT_DEVICE_ROLE"
            collector.add(
                Severity.ERROR,
                Code.DEVICE_ROLE_NOT_FOUND,
                line=row.line,
                column="device_role",
                value=wanted,
                expected="the name or slug of a device role that already exists in NetBox",
                message=(
                    f"No device role {wanted!r} exists in NetBox, and it is what {source} asks "
                    f"for. This importer never creates roles: create it first."
                ),
            )
            planned.outcome = RowOutcome.BLOCKED
            return None

        self._register(
            state, registry, ROLES, Registry.key(role.get("slug")), role, f"id={role['id']}"
        )
        planned.resolved["device_role"] = f"{role.get('name')} (existing)"
        return int(role["id"])

    def _tenant(
        self,
        row: Row,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> int | None:
        """The tenant's id, or None when the column is blank. A missing tenant blocks."""
        name = row.get("tenant").strip()
        if not name:
            return None

        tenant = lookups.get_or_none(TENANTS, name=name) or lookups.get_or_none(
            TENANTS, slug=derive_slug(name)
        )
        if tenant is None:
            collector.add(
                Severity.ERROR,
                Code.TENANT_NOT_FOUND,
                line=row.line,
                column="tenant",
                value=name,
                expected="the name of a tenant that already exists in NetBox",
                message=(
                    f"No tenant named {name!r} exists in NetBox. This importer never creates "
                    f"tenants: create it first, or clear the column."
                ),
            )
            planned.outcome = RowOutcome.BLOCKED
            return None

        self._register(
            state, registry, TENANTS, Registry.key(tenant.get("slug")), tenant, f"id={tenant['id']}"
        )
        planned.resolved["tenant"] = f"{tenant.get('name')} (existing)"
        return int(tenant["id"])

    def _check_asset_tag(
        self,
        row: Row,
        existing: dict[str, Any] | None,
        lookups: Lookups,
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> bool:
        """Requirement 11.6: a tag held by a *different* object is a collision, and it blocks."""
        tag = row.get("asset_tag").strip()
        if not tag:
            return False
        owner = self._asset_tag_owners.get(fold(tag))
        if owner is None:
            return False
        if existing is not None and int(owner["id"]) == int(existing["id"]):
            return False

        collector.add(
            Severity.ERROR,
            Code.UNIQUE_FIELD_TAKEN,
            line=row.line,
            column="asset_tag",
            value=tag,
            expected="an asset tag no other object in NetBox holds",
            message=(
                f"Asset tag {tag!r} already belongs to {owner.get('name')!r} in NetBox. An asset "
                f"tag is unique across the instance, so this row cannot also carry it."
            ),
            related_object=f"Device {owner.get('name')}",
            related_url=self._url(lookups, DEVICES, owner),
        )
        planned.outcome = RowOutcome.BLOCKED
        return True

    # --- the device itself --------------------------------------------------------------------

    def _already_present(
        self,
        row: Row,
        existing: dict[str, Any],
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> None:
        """Requirements 8.10, 11.2: the device is not recreated; remaining IP work still is."""
        specs = self.specs()
        label = f"Device {existing.get('name')}"
        url = self._url(lookups, DEVICES, existing)
        device_type = existing.get("device_type") or {}

        taken = self._check_asset_tag(row, existing, lookups, collector, planned)
        comparisons: list[tuple[str, Any]] = [
            ("rack", existing.get("rack")),
            ("rack_face", existing.get("face")),
            ("bottom_ru", existing.get("position")),
            ("model", device_type),
            ("device_role", existing.get("role")),
            ("status", existing.get("status")),
            ("serial", existing.get("serial")),
            ("tenant", existing.get("tenant")),
        ]
        if device_type.get("u_height") is not None:
            comparisons.append(("ru_height", device_type["u_height"]))
        if not taken:
            # Requirement 11.6: the collision above already says everything a difference in
            # bookkeeping would, and says it more precisely.
            comparisons.append(("asset_tag", existing.get("asset_tag")))

        for key, held in comparisons:
            conflict = compare_to_existing(
                specs[key], row.get(key), held, object_label=label, related_url=url
            )
            if conflict is None:
                continue
            collector.add_row_error(conflict, row.line)
            if conflict.severity is Severity.ERROR:
                planned.outcome = RowOutcome.BLOCKED

        self._register(
            state,
            registry,
            DEVICES,
            Registry.key(existing.get("name")),
            existing,
            f"id={existing['id']},rack={(existing.get('rack') or {}).get('id')},"
            f"position={netbox_text(existing.get('position'))},"
            f"face={netbox_text(existing.get('face'))},"
            f"type={(device_type or {}).get('id')},"
            f"primary_ip4={netbox_text(existing.get('primary_ip4'))},"
            f"primary_ip6={netbox_text(existing.get('primary_ip6'))}",
        )
        planned.resolved["device_name"] = f"{existing.get('name')} (existing)"
        planned.resolved["object"] = str(existing.get("name") or "")

        remaining = False
        if planned.outcome != RowOutcome.BLOCKED:
            remaining = self._plan_addresses(
                row,
                lookups,
                registry,
                state,
                collector,
                planned,
                device_name=str(existing.get("name") or row.get("device_name")),
                existing=existing,
                device_type=(
                    existing.get("device_type")
                    if isinstance(existing.get("device_type"), dict)
                    else None
                ),
                device_ref=int(existing["id"]),
            )
        if planned.outcome == RowOutcome.BLOCKED:
            return
        if remaining:
            planned.outcome = RowOutcome.CREATE
            return
        planned.outcome = RowOutcome.SKIP_EXISTS
        collector.add(
            Severity.INFO,
            Code.ALREADY_EXISTS,
            line=row.line,
            column="device_name",
            value=str(existing.get("name") or ""),
            message=(
                f"Device {existing.get('name')!r} already exists in NetBox. Nothing to create for "
                f"this row, and nothing about it will be changed."
            ),
            related_object=label,
            related_url=url,
        )

    def _plan_addresses(
        self,
        row: Row,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
        *,
        device_name: str,
        existing: dict[str, Any] | None,
        device_type: dict[str, Any] | None,
        device_ref: Any,
    ) -> bool:
        """Create missing addresses and PATCH primary IP (Requirements 8.14–8.21).

        Returns True when this row still has writes. A fully-matching existing device returns False
        so the caller can report already-present.
        """
        addresses = parse_row_addresses(row.get)
        type_id = related_id(device_type) if device_type is not None else None
        if type_id is None and existing is not None:
            type_id = related_id(existing.get("device_type"))
        templates = self._templates_by_type.get(type_id or -1, [])
        type_roles = resolve_roles(templates)

        if existing is not None:
            ifaces = list(lookups.terminations_for(int(existing["id"]), "interface").values())
            device_roles = resolve_roles(ifaces)
        else:
            ifaces = []
            device_roles = type_roles

        defines = any(
            match.found or match.ambiguous
            for match in (type_roles["management"], type_roles["loopback"])
        )
        has_on_device = any(
            match.found or match.ambiguous
            for match in (device_roles["management"], device_roles["loopback"])
        )
        need_ip = has_on_device if existing is not None else defines

        if need_ip and not addresses:
            collector.add(
                Severity.ERROR,
                Code.DEVICE_IP_REQUIRED,
                line=row.line,
                column="management_ip4",
                value="(blank)",
                expected="at least one of the four address cells",
                message=(
                    f"{device_name!r} is a type that defines a management or loopback interface, "
                    f"so at least one of the four address cells must be filled."
                ),
            )
            planned.outcome = RowOutcome.BLOCKED
            return False

        if not addresses:
            return False

        roles_needed = {item.role for item in addresses}
        iface_for_role: dict[str, Any] = {}
        create_iface_for_role: dict[str, dict[str, Any]] = {}
        for role in roles_needed:
            live = device_roles[role]
            typed = type_roles[role]
            column = next(item.column for item in addresses if item.role == role)
            if live.ambiguous or typed.ambiguous:
                names = ", ".join(live.candidates or typed.candidates or [])
                collector.add(
                    Severity.ERROR,
                    AMBIGUOUS[role],
                    line=row.line,
                    column=column,
                    value=row.get(column),
                    expected=f"exactly one {role} interface on the device type",
                    message=(
                        f"{role.capitalize()} is ambiguous on this type: {names}. Mark exactly "
                        f"one interface as "
                        + (
                            "mgmt_only"
                            if role == "management"
                            else "type loopback"
                        )
                        + " before importing."
                    ),
                )
                planned.outcome = RowOutcome.BLOCKED
                continue
            if live.found:
                iface_for_role[role] = live.record
                object_id = live.record.get("id")
                if object_id is not None:
                    registry.remember(
                        INTERFACES, Registry.key(device_name, live.name), int(object_id)
                    )
                continue
            if typed.found and existing is not None:
                # The type defines the port; this device was created before the template existed.
                # Creating that one interface is remaining work so the address can land (8.20).
                create_iface_for_role[role] = typed.record
                iface_for_role[role] = typed.record
                continue
            if typed.found:
                iface_for_role[role] = typed.record
                continue
            collector.add(
                Severity.ERROR,
                NOT_FOUND[role] if defines or typed.found else Code.DEVICE_IP_NOT_APPLICABLE,
                line=row.line,
                column=column,
                value=row.get(column),
                expected=f"a unique {role} interface on the device type",
                message=(
                    f"Nowhere to assign {column} on {device_name!r}. Define a unique "
                    f"{'mgmt_only' if role == 'management' else 'type=loopback'} interface on "
                    f"the device type, then re-upload."
                ),
            )
            planned.outcome = RowOutcome.BLOCKED

        if planned.outcome == RowOutcome.BLOCKED:
            return False

        for template in create_iface_for_role.values():
            planned.actions.append(
                CreateAction(
                    endpoint=INTERFACES,
                    payload={
                        "device": int(existing["id"]) if existing is not None else device_ref,
                        "name": template.get("name"),
                        "type": choice_value(template.get("type")),
                        "mgmt_only": bool(template.get("mgmt_only")),
                    },
                    key=Registry.key(device_name, template.get("name")),
                    label=f"{device_name} {template.get('name')}",
                    line=row.line,
                    object_kind="interface",
                )
            )

        added = bool(create_iface_for_role)
        for addr in addresses:
            iface = iface_for_role[addr.role]
            iface_name = str(iface.get("name") or "")
            action = self._address_action(
                row,
                lookups,
                registry,
                state,
                collector,
                planned,
                device_name=device_name,
                existing=existing,
                addr=addr,
                iface=iface,
                iface_name=iface_name,
            )
            if planned.outcome == RowOutcome.BLOCKED:
                return False
            if action is not None:
                planned.actions.append(action)
                added = True
            planned.resolved[addr.column] = f"{addr.cidr} on {iface_name}"

        patch = self._primary_patch(
            row,
            lookups,
            registry,
            collector,
            planned,
            device_name=device_name,
            existing=existing,
            device_ref=device_ref,
            addresses=addresses,
        )
        if planned.outcome == RowOutcome.BLOCKED:
            return False
        if patch is not None:
            planned.actions.append(patch)
            added = True
        ip4_col, ip6_col = primary_columns(addresses)
        by_col = {item.column: item for item in addresses}
        if ip4_col:
            planned.resolved["primary_ip4"] = by_col[ip4_col].cidr
        if ip6_col:
            planned.resolved["primary_ip6"] = by_col[ip6_col].cidr
        shown = displayed_primary(ip4_col, ip6_col)
        if shown:
            planned.resolved["displayed_primary"] = shown
        return added

    def _address_action(
        self,
        row: Row,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
        *,
        device_name: str,
        existing: dict[str, Any] | None,
        addr: ParsedAddress,
        iface: dict[str, Any],
        iface_name: str,
    ) -> CreateAction | None:
        held = self._ip_record(lookups, addr.cidr)
        iface_id = iface.get("id")
        assigned: Any
        if existing is not None and iface_id is not None:
            assigned = int(iface_id)
        else:
            assigned = Ref(INTERFACES, Registry.key(device_name, iface_name))

        if held is None:
            state[f"{IP_ADDRESSES}:{addr.cidr}"] = "absent"
            return CreateAction(
                endpoint=IP_ADDRESSES,
                payload={
                    "address": addr.cidr,
                    "status": "active",
                    "assigned_object_type": "dcim.interface",
                    "assigned_object_id": assigned,
                },
                key=Registry.key(addr.cidr),
                label=f"{addr.cidr} on {device_name} {iface_name}",
                line=row.line,
                object_kind="ip address",
            )

        object_id = int(held["id"])
        registry.remember(IP_ADDRESSES, Registry.key(addr.cidr), object_id)
        obj_type = str(held.get("assigned_object_type") or "")
        obj_id = held.get("assigned_object_id")
        state[f"{IP_ADDRESSES}:{addr.cidr}"] = f"id={object_id},assigned={obj_type}:{obj_id}"

        same_iface = (
            obj_type == "dcim.interface"
            and iface_id is not None
            and obj_id is not None
            and int(obj_id) == int(iface_id)
        )
        if same_iface:
            return None

        holder = self._ip_holder_label(held)
        collector.add(
            Severity.ERROR,
            Code.IP_ALREADY_ASSIGNED,
            line=row.line,
            column=addr.column,
            value=addr.cidr,
            expected="an address that is free, or already on this interface",
            message=(
                f"{addr.cidr} already exists in NetBox"
                + (f" ({holder})" if holder else "")
                + ". It cannot be assigned here."
            ),
            related_object=holder or None,
        )
        planned.outcome = RowOutcome.BLOCKED
        return None

    def _primary_patch(
        self,
        row: Row,
        lookups: Lookups,
        registry: Registry,
        collector: IssueCollector,
        planned: PlannedRow,
        *,
        device_name: str,
        existing: dict[str, Any] | None,
        device_ref: Any,
        addresses: list[ParsedAddress],
    ) -> CreateAction | None:
        ip4_col, ip6_col = primary_columns(addresses)
        if ip4_col is None and ip6_col is None:
            return None
        by_col = {item.column: item for item in addresses}
        payload: dict[str, Any] = {
            "id": int(existing["id"]) if existing is not None else device_ref
        }

        def intended(column: str) -> Any:
            cidr = by_col[column].cidr
            found = registry.id_for(IP_ADDRESSES, Registry.key(cidr))
            return found if found is not None else Ref(IP_ADDRESSES, Registry.key(cidr))

        if existing is not None:
            for field, column in (("primary_ip4", ip4_col), ("primary_ip6", ip6_col)):
                if column is None:
                    continue
                held = existing.get(field)
                held_addr = held.get("address") if isinstance(held, dict) else None
                if not held or not held_addr:
                    payload[field] = intended(column)
                    continue
                try:
                    matches = canonical(held_addr) == canonical(by_col[column].cidr)
                except ValueError:
                    matches = False
                if matches:
                    continue
                collector.add(
                    Severity.ERROR,
                    Code.EXISTS_BUT_DIFFERS,
                    line=row.line,
                    column=column,
                    value=by_col[column].cidr,
                    expected=str(held_addr),
                    message=(
                        f"Device {device_name!r} already has {field} {held_addr!r}, which is not "
                        f"{by_col[column].cidr}. Primary IP is not overwritten."
                    ),
                    related_object=f"Device {device_name}",
                    related_url=self._url(lookups, DEVICES, existing),
                )
                planned.outcome = RowOutcome.BLOCKED
                return None
        else:
            if ip4_col:
                payload["primary_ip4"] = intended(ip4_col)
            if ip6_col:
                payload["primary_ip6"] = intended(ip6_col)

        if "primary_ip4" not in payload and "primary_ip6" not in payload:
            return None
        return CreateAction(
            endpoint=DEVICES,
            payload=payload,
            key=Registry.key(device_name, "primary"),
            label=f"{device_name} primary IP",
            line=row.line,
            object_kind="device primary IP",
            method="patch",
        )

    def _ip_record(self, lookups: Lookups, cidr: str) -> dict[str, Any] | None:
        wanted = canonical(cidr)
        for record in lookups.all_records(IP_ADDRESSES):
            held = record.get("address")
            if not held:
                continue
            try:
                if canonical(held) == wanted:
                    return record
            except ValueError:
                continue
        return None

    @staticmethod
    def _ip_holder_label(record: dict[str, Any]) -> str:
        assigned = record.get("assigned_object") or {}
        if isinstance(assigned, dict) and assigned.get("name"):
            device = assigned.get("device") or {}
            device_name = device.get("name") if isinstance(device, dict) else ""
            place = f"{device_name} {assigned.get('name')}".strip()
            return f"assigned to {place}" if place else "assigned elsewhere"
        if record.get("assigned_object_type") and record.get("assigned_object_id"):
            return (
                f"assigned to {record['assigned_object_type']} "
                f"{record['assigned_object_id']}"
            )
        return "unassigned"

    def after_phase(
        self,
        endpoint: str,
        method: str,
        registry: Registry,
        lookups: Lookups,
    ) -> None:
        """After devices are created, index the interfaces NetBox inherited from the type."""
        if endpoint != DEVICES or method != "post":
            return
        for key, object_id in registry.remembered(DEVICES):
            for iface in lookups.client.get_all(INTERFACES, device_id=[object_id]):
                registry.remember(
                    INTERFACES, Registry.key(*key, iface.get("name")), int(iface["id"])
                )

    def _device_action(
        self,
        row: Row,
        name: str,
        rack: dict[str, Any],
        device_type: TypeResolution,
        role_id: int,
        tenant_id: int | None,
    ) -> CreateAction:
        payload: dict[str, Any] = {
            "name": name,
            "device_type": device_type.reference,
            "role": role_id,
            # NetBox wants the site as well as the rack, and the rack is the authority on which
            # site that is — the device sheet never names one.
            "site": (rack.get("site") or {}).get("id"),
            "rack": int(rack["id"]),
            # Requirement 8.3: the lowest unit occupied is what NetBox calls the position.
            "position": int(row.get("bottom_ru")),
            "face": fold(row.get("rack_face")),
            # Requirement 8.9.
            "status": fold(row.get("status")) or DEFAULT_STATUS,
        }
        for column in ("serial", "asset_tag"):
            value = row.get(column).strip()
            if value:
                payload[column] = value
        if tenant_id:
            payload["tenant"] = tenant_id

        return CreateAction(
            endpoint=DEVICES,
            payload=payload,
            key=Registry.key(name),
            label=name,
            line=row.line,
            object_kind="device",
        )

    @staticmethod
    def _url(lookups: Lookups, endpoint: str, existing: dict[str, Any]) -> str:
        object_id = existing.get("id")
        return lookups.client.object_url(endpoint, int(object_id)) if object_id else ""
