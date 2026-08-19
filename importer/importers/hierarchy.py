# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Sheet 1 — one row per rack, creating the hierarchy above it (Requirement 7).

The mapping is decision D1: `location` → `dcim.Region`, `datacenter` → `dcim.Site`, `datahall` →
`dcim.Location`, and `rack` → `dcim.Rack`. NetBox requires a slug on the first three, and the sheet
does not ask for one, so it is derived from the name (Requirement 7.3).

This is the only importer that creates its own parents, which is why it works against a completely
empty NetBox (Requirement 17.7). Parents are created before children because the child's payload
carries a `Ref` to the parent, and `endpoint_order` posts regions, then sites, then locations, then
racks.

One subtlety about conflicts. `columns.py` classifies the `location`, `datacenter`, and `datahall`
columns as IDENTITY, because they are how an object is found: a different datacenter name is a
different site, not a site with a wrong name. But Requirement 11.5 also names *relationships* —
a site's region, a location's site — as consequential, and a relationship is not a column. Those are
therefore checked explicitly below rather than through `compare_to_existing`, and they block.
"""

from dataclasses import dataclass
from typing import Any, ClassVar

from importer.importers.base import (
    BaseImporter,
    CreateAction,
    PlannedRow,
    Ref,
    Registry,
    RowOutcome,
)
from importer.naming import derive_slug, parse_rack_name
from importer.services.lookups import Lookups
from importer.services.sheets import Row
from importer.validation import (
    Code,
    IssueCollector,
    RowError,
    Severity,
    compare_to_existing,
)

REGIONS = "dcim.regions"
SITES = "dcim.sites"
LOCATIONS = "dcim.locations"
RACKS = "dcim.racks"
TENANTS = "tenancy.tenants"

DEFAULT_WIDTH = 19
DEFAULT_STATUS = "active"


@dataclass
class Resolution:
    """What one level of the hierarchy resolved to for one row."""

    endpoint: str
    key: tuple[Any, ...]
    label: str
    existing: dict[str, Any] | None = None
    action: CreateAction | None = None

    @property
    def ref(self) -> Ref:
        return Ref(self.endpoint, self.key)

    @property
    def object_id(self) -> int | None:
        return self.existing.get("id") if self.existing else None


class HierarchyImporter(BaseImporter):
    name = "hierarchy"
    endpoint_order = (REGIONS, SITES, LOCATIONS, RACKS)

    choice_sources: ClassVar[dict[str, tuple[str, str]]] = {
        "rack_status": (RACKS, "status"),
        "rack_width": (RACKS, "width"),
        "rack_form_factor": (RACKS, "form_factor"),
    }

    # --- S2 ---------------------------------------------------------------------------------

    def extra_cell_checks(self, row: Row) -> list[RowError]:
        errors: list[RowError] = []
        try:
            parse_rack_name(row.get("rack"))
        except RowError as error:
            errors.append(error)

        for key in ("location", "datacenter", "datahall"):
            value = row.get(key)
            if value and not derive_slug(value):
                errors.append(
                    RowError(
                        Code.SLUG_NOT_DERIVABLE,
                        column=key,
                        value=value,
                        expected="a name containing at least one letter or digit",
                        message=(
                            f"No URL-safe slug can be derived from {value!r}, and NetBox requires "
                            f"one. Give it a name with letters or digits."
                        ),
                    )
                )
        return errors

    # --- S3 ---------------------------------------------------------------------------------

    def check_consistency(self, rows: list[Row], collector: IssueCollector) -> None:
        seen_racks: dict[str, int] = {}
        slug_owners: dict[tuple[str, str], tuple[str, int]] = {}

        for row in rows:
            try:
                rack_name = parse_rack_name(row.get("rack"))
            except RowError:
                continue  # already reported in S2

            # Requirement 6.3: the rack name must agree with its own row.
            if not rack_name.agrees_with(
                datacenter=row.get("datacenter"), datahall=row.get("datahall")
            ):
                collector.add(
                    Severity.ERROR,
                    Code.RACK_NAME_DISAGREES_WITH_COLUMNS,
                    line=row.line,
                    column="rack",
                    value=rack_name.raw,
                    expected=(f"{row.get('datacenter')}-{row.get('datahall')}-{rack_name.rack}"),
                    message=(
                        f"Rack name {rack_name.raw!r} says datacenter "
                        f"{rack_name.datacenter!r} and datahall {rack_name.datahall!r}, but this "
                        f"row's columns say {row.get('datacenter')!r} and "
                        f"{row.get('datahall')!r}. One of them is wrong."
                    ),
                )

            # Requirement 7.8: a duplicate rack name names the earlier row.
            folded = rack_name.key
            if folded in seen_racks:
                collector.add(
                    Severity.ERROR,
                    Code.DUPLICATE_RACK_IN_FILE,
                    line=row.line,
                    column="rack",
                    value=rack_name.raw,
                    expected="one row per rack",
                    message=(
                        f"Rack {rack_name.raw!r} is already declared on line "
                        f"{seen_racks[folded]}. Remove the duplicate row."
                    ),
                    related_line=seen_racks[folded],
                )
            else:
                seen_racks[folded] = row.line

            # Two differently spelled names that slug identically would collide inside NetBox, which
            # would otherwise surface as an opaque 400 partway through the apply.
            for kind, column in (("region", "location"), ("site", "datacenter")):
                value = row.get(column)
                if not value:
                    continue
                slug = derive_slug(value)
                owner = slug_owners.get((kind, slug))
                if owner is None:
                    slug_owners[(kind, slug)] = (value, row.line)
                elif owner[0].strip().casefold() != value.strip().casefold():
                    collector.add(
                        Severity.ERROR,
                        Code.SLUG_COLLISION,
                        line=row.line,
                        column=column,
                        value=value,
                        expected=f"a name whose slug differs from {owner[0]!r}",
                        message=(
                            f"{value!r} and {owner[0]!r} (line {owner[1]}) both reduce to the slug "
                            f"{slug!r}, so NetBox would treat them as one {kind}. Rename one."
                        ),
                        related_line=owner[1],
                    )

    # --- S4 ---------------------------------------------------------------------------------

    def prefetch(self, rows: list[Row], lookups: Lookups) -> None:
        regions = [row.get("location") for row in rows]
        sites = [row.get("datacenter") for row in rows]
        halls = [row.get("datahall") for row in rows]
        racks = [row.get("rack") for row in rows]
        tenants = [row.get("tenant") for row in rows if row.get("tenant")]

        lookups.prefetch(REGIONS, names=regions)
        lookups.prefetch(SITES, names=sites)
        lookups.prefetch(LOCATIONS, names=halls)
        lookups.prefetch(RACKS, names=racks)
        if tenants:
            lookups.prefetch(TENANTS, names=tenants)

        # Slug collisions are found by slug, not by name, so the candidates have to be loaded too
        # (Requirement 7.3).
        for endpoint, values in ((REGIONS, regions), (SITES, sites), (LOCATIONS, halls)):
            slugs = sorted({derive_slug(value) for value in values if value})
            if slugs:
                lookups.prefetch(endpoint, slug__ie=slugs)

    def plan_row(
        self,
        row: Row,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
    ) -> PlannedRow:
        planned = PlannedRow(line=row.line)
        try:
            rack_name = parse_rack_name(row.get("rack"))
        except RowError as error:
            collector.add_row_error(error, row.line)
            planned.outcome = RowOutcome.BLOCKED
            return planned

        region = self._region(row, lookups, registry, state, collector, planned)
        site = self._site(row, region, lookups, registry, state, collector, planned)
        hall = self._location(row, site, lookups, registry, state, collector, planned)
        rack = self._rack(row, rack_name, site, hall, lookups, registry, state, collector, planned)

        for resolution in (region, site, hall, rack):
            if resolution is not None and resolution.action is not None:
                planned.actions.append(resolution.action)

        if planned.outcome != RowOutcome.BLOCKED:
            planned.outcome = RowOutcome.CREATE if planned.actions else RowOutcome.SKIP_EXISTS
            if not planned.actions and rack is not None:
                collector.add(
                    Severity.INFO,
                    Code.ALREADY_EXISTS,
                    line=row.line,
                    column="rack",
                    value=rack_name.raw,
                    message=(
                        f"Rack {rack_name.raw!r} and everything above it already exist. "
                        f"Nothing to create for this row."
                    ),
                    related_object=rack.label,
                )
        return planned

    # --- one level at a time ----------------------------------------------------------------

    def _register(
        self,
        state: dict[str, str],
        registry: Registry,
        resolution: Resolution,
        signature: str,
    ) -> None:
        """Record an existing object's id and the state the plan depends on (Requirement 10.14)."""
        if resolution.existing is None:
            return
        registry.remember(resolution.endpoint, resolution.key, int(resolution.existing["id"]))
        state[f"{resolution.endpoint}:{'/'.join(str(part) for part in resolution.key)}"] = signature

    def _region(
        self,
        row: Row,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> Resolution:
        name = row.get("location").strip()
        slug = derive_slug(name)
        resolution = Resolution(endpoint=REGIONS, key=Registry.key(slug), label=f"Region {name}")

        existing = lookups.get_or_none(REGIONS, name=name) or lookups.get_or_none(
            REGIONS, slug=slug
        )
        if existing is not None:
            if not self._slug_matches(existing, name, slug, "location", row, collector, "region"):
                planned.outcome = RowOutcome.BLOCKED
                return resolution
            resolution.existing = existing
            self._register(state, registry, resolution, f"id={existing['id']}")
            planned.resolved["location"] = f"{existing.get('name')} (existing)"
            return resolution

        resolution.action = CreateAction(
            endpoint=REGIONS,
            payload={"name": name, "slug": slug},
            key=resolution.key,
            label=f"Region {name}",
            line=row.line,
            object_kind="region",
        )
        planned.resolved["location"] = f"{name} (create)"
        return resolution

    def _site(
        self,
        row: Row,
        region: Resolution,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> Resolution:
        name = row.get("datacenter").strip()
        slug = derive_slug(name)
        resolution = Resolution(endpoint=SITES, key=Registry.key(slug), label=f"Site {name}")

        existing = lookups.get_or_none(SITES, name=name) or lookups.get_or_none(SITES, slug=slug)
        if existing is not None:
            if not self._slug_matches(existing, name, slug, "datacenter", row, collector, "site"):
                planned.outcome = RowOutcome.BLOCKED
                return resolution

            # Requirement 11.5: a site's region is consequential, so a site sitting under a
            # different region blocks rather than being silently re-parented (Requirement 11.3).
            held = (existing.get("region") or {}).get("name") or "(none)"
            wanted = row.get("location").strip()
            if held.strip().casefold() != wanted.strip().casefold():
                collector.add(
                    Severity.ERROR,
                    Code.EXISTS_BUT_DIFFERS,
                    line=row.line,
                    column="location",
                    value=wanted,
                    expected=held,
                    message=(
                        f"Site {name!r} already exists in NetBox under region {held!r}, but this "
                        f"row puts it in {wanted!r}. This iteration never re-parents an existing "
                        f"object, and every rack below it would inherit the wrong location."
                    ),
                    related_object=f"Site {existing.get('name')}",
                    related_url=self._url(lookups, SITES, existing),
                )
                planned.outcome = RowOutcome.BLOCKED
                return resolution

            resolution.existing = existing
            self._register(
                state,
                registry,
                resolution,
                f"id={existing['id']},region={(existing.get('region') or {}).get('id')}",
            )
            planned.resolved["datacenter"] = f"{existing.get('name')} (existing)"
            return resolution

        resolution.action = CreateAction(
            endpoint=SITES,
            payload={"name": name, "slug": slug, "region": region.ref},
            key=resolution.key,
            label=f"Site {name}",
            line=row.line,
            object_kind="site",
        )
        planned.resolved["datacenter"] = f"{name} (create)"
        return resolution

    def _location(
        self,
        row: Row,
        site: Resolution,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> Resolution:
        name = row.get("datahall").strip()
        slug = derive_slug(name)
        site_id = site.object_id
        # Requirement 7.9: a datahall is scoped to its site, so the key carries the site.
        resolution = Resolution(
            endpoint=LOCATIONS,
            key=Registry.key(site.key[0], slug),
            label=f"Datahall {name}",
        )

        existing = (
            lookups.get_or_none(LOCATIONS, site=site_id, name=name)
            or lookups.get_or_none(LOCATIONS, site=site_id, slug=slug)
            if site_id is not None
            else None
        )
        if existing is not None:
            resolution.existing = existing
            self._register(
                state,
                registry,
                resolution,
                f"id={existing['id']},site={(existing.get('site') or {}).get('id')}",
            )
            planned.resolved["datahall"] = f"{existing.get('name')} (existing)"
            return resolution

        resolution.action = CreateAction(
            endpoint=LOCATIONS,
            payload={"name": name, "slug": slug, "site": site.ref},
            key=resolution.key,
            label=f"Datahall {name}",
            line=row.line,
            object_kind="location",
        )
        planned.resolved["datahall"] = f"{name} (create)"
        return resolution

    def _rack(
        self,
        row: Row,
        rack_name: Any,
        site: Resolution,
        hall: Resolution,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> Resolution:
        specs = self.specs()
        resolution = Resolution(
            endpoint=RACKS,
            key=Registry.key(rack_name.raw),
            label=f"Rack {rack_name.raw}",
        )

        tenant_id = self._tenant(row, lookups, collector, planned)
        if tenant_id is False:
            planned.outcome = RowOutcome.BLOCKED
            return resolution

        existing = lookups.get_or_none(RACKS, name=rack_name.raw)
        if existing is not None:
            resolution.existing = existing
            # Requirement 11.5: the rack's own site and location are consequential.
            for column, related in (("datacenter", "site"), ("datahall", "location")):
                held = (existing.get(related) or {}).get("name") or "(none)"
                wanted = row.get(column).strip()
                if held.strip().casefold() != wanted.strip().casefold():
                    collector.add(
                        Severity.ERROR,
                        Code.EXISTS_BUT_DIFFERS,
                        line=row.line,
                        column=column,
                        value=wanted,
                        expected=held,
                        message=(
                            f"Rack {rack_name.raw!r} already exists in NetBox with {related} "
                            f"{held!r}, but this row says {wanted!r}. An existing rack is never "
                            f"moved by this tool."
                        ),
                        related_object=f"Rack {existing.get('name')}",
                        related_url=self._url(lookups, RACKS, existing),
                    )
                    planned.outcome = RowOutcome.BLOCKED

            comparisons = (
                ("rack_u_height", existing.get("u_height")),
                ("rack_width", existing.get("width")),
                ("rack_status", existing.get("status")),
                ("rack_form_factor", existing.get("form_factor")),
                ("tenant", existing.get("tenant")),
                ("description", existing.get("description")),
            )
            for key, held in comparisons:
                conflict = compare_to_existing(
                    specs[key],
                    row.get(key),
                    held,
                    object_label=f"Rack {existing.get('name')}",
                    related_url=self._url(lookups, RACKS, existing),
                )
                if conflict is not None:
                    collector.add_row_error(conflict, row.line)
                    if conflict.severity is Severity.ERROR:
                        planned.outcome = RowOutcome.BLOCKED

            self._register(
                state,
                registry,
                resolution,
                f"id={existing['id']},u_height={existing.get('u_height')},"
                f"site={(existing.get('site') or {}).get('id')},"
                f"location={(existing.get('location') or {}).get('id')}",
            )
            planned.resolved["rack"] = f"{existing.get('name')} (existing)"
            return resolution

        payload: dict[str, Any] = {
            # Requirement 6.4: created with the casing the sheet supplied.
            "name": rack_name.raw,
            "site": site.ref,
            "location": hall.ref,
            "u_height": int(row.get("rack_u_height")),
            # Requirement 7.7: both optional columns have defaults.
            "width": int(row.get("rack_width") or DEFAULT_WIDTH),
            "status": (row.get("rack_status") or DEFAULT_STATUS).strip().casefold(),
        }
        if row.get("rack_form_factor"):
            payload["form_factor"] = row.get("rack_form_factor").strip().casefold()
        if row.get("description"):
            payload["description"] = row.get("description").strip()
        if tenant_id:
            payload["tenant"] = tenant_id

        resolution.action = CreateAction(
            endpoint=RACKS,
            payload=payload,
            key=resolution.key,
            label=rack_name.raw,
            line=row.line,
            object_kind="rack",
        )
        planned.resolved["rack"] = f"{rack_name.raw} (create)"
        return resolution

    def _tenant(
        self,
        row: Row,
        lookups: Lookups,
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> int | None | bool:
        """The tenant's id, None when the column is blank, or False when it does not exist."""
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
            return False
        planned.resolved["tenant"] = f"{tenant.get('name')} (existing)"
        return int(tenant["id"])

    def _slug_matches(
        self,
        existing: dict[str, Any],
        name: str,
        slug: str,
        column: str,
        row: Row,
        collector: IssueCollector,
        kind: str,
    ) -> bool:
        """Requirement 7.3: a derived slug that belongs to a differently named object blocks."""
        if str(existing.get("name", "")).strip().casefold() == name.strip().casefold():
            return True
        if str(existing.get("slug", "")) != slug:
            return True
        collector.add(
            Severity.ERROR,
            Code.SLUG_COLLISION,
            line=row.line,
            column=column,
            value=name,
            expected=f"a name that does not reduce to the slug {slug!r}",
            message=(
                f"The slug derived from {name!r} is {slug!r}, which already belongs to the "
                f"{kind} {existing.get('name')!r}. NetBox requires slugs to be unique, so this "
                f"row would either fail or attach to the wrong object. Rename one of them."
            ),
            related_object=f"{kind} {existing.get('name')}",
        )
        return False

    @staticmethod
    def _url(lookups: Lookups, endpoint: str, existing: dict[str, Any]) -> str:
        object_id = existing.get("id")
        return lookups.client.object_url(endpoint, int(object_id)) if object_id else ""
