# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Sheet 1 — one row per rack, creating the hierarchy above it (Requirement 7).

The mapping is decision D1: `location` → `dcim.Region`, `datacenter` → `dcim.Site`, `datahall` →
`dcim.Location`, and `rack` → `dcim.Rack`. NetBox requires a slug on the first three.

**Where those slugs come from (decision D9).** The datacenter's and the datahall's slugs are the
rack name's first two segments, so the sheet supplies them in optional `datacenter_slug` and
`datahall_slug` columns; a blank cell falls back to a slug derived from the name, which is what the
tool always did and what keeps a sheet written before those columns valid. The region has no slug
column, because a region's slug appears in no rack name and so in nothing the operator can see.

Whichever way it was arrived at, one value — the *effective slug* of Requirement 7.10 — is used for
the NetBox lookup, for the created object's `slug`, and for the rack-name agreement check, so those
three readings of "the slug" cannot drift apart.

The supplied-versus-derived distinction matters in exactly one place, and it is the crux of D9: a
**supplied** slug that disagrees with an object already in NetBox blocks (Requirement 7.13, through
the column's CONSEQUENTIAL classification), while a **derived** one does not (Requirement 7.14). The
fallback is the tool's guess, and an object created outside this tool has no reason to match it;
blocking on that would reject rows that pass today.

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
from importer.naming import build_rack_name, derive_slug, effective_slug, parse_rack_name
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

# The two levels whose slug a rack name is built from (D9), as (name column, slug column).
SLUGGED_LEVELS: tuple[tuple[str, str], ...] = (
    ("datacenter", "datacenter_slug"),
    ("datahall", "datahall_slug"),
)


def row_slug(row: Row, name_column: str, slug_column: str) -> str:
    """The effective slug for one level of one row (Requirement 7.10). May raise `RowError`."""
    return effective_slug(
        row.get(name_column),
        row.get(slug_column),
        column=slug_column,
        name_column=name_column,
    )


def blame_column(row: Row, name_column: str, slug_column: str) -> str:
    """Where a slug problem is reported: the cell the operator would have to edit (Req 7.11)."""
    return slug_column if row.get(slug_column).strip() else name_column


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

    def extra_cell_checks(self, row: Row, lookups: Lookups) -> list[RowError]:
        errors: list[RowError] = []
        try:
            parse_rack_name(row.get("rack"))
        except RowError as error:
            errors.append(error)

        # The region has no slug column, so its slug only has to exist: it is never part of a rack
        # name, and nothing the operator reads depends on it (Requirement 7.3).
        region = row.get("location")
        if region and not derive_slug(region):
            errors.append(
                RowError(
                    Code.SLUG_NOT_DERIVABLE,
                    column="location",
                    value=region,
                    expected="a name containing at least one letter or digit",
                    message=(
                        f"No URL-safe slug can be derived from {region!r}, and NetBox requires "
                        f"one. Give it a name with letters or digits."
                    ),
                )
            )

        # The other two carry the rack name's segments, so their slugs are validated in full
        # (Requirements 6.6, 7.11). A blank name cell is already reported as a missing required
        # value; complaining about its slug as well would be two errors for one mistake.
        for name_column, slug_column in SLUGGED_LEVELS:
            if not row.get(name_column).strip():
                continue
            try:
                row_slug(row, name_column, slug_column)
            except RowError as error:
                errors.append(error)
        return errors

    # --- S3 ---------------------------------------------------------------------------------

    def check_consistency(self, rows: list[Row], collector: IssueCollector) -> None:
        seen_racks: dict[str, int] = {}
        region_slugs: dict[str, tuple[str, int]] = {}
        # Both directions of the name↔slug relationship, keyed by scope so that datahall slugs are
        # compared within a datacenter rather than globally (Requirements 7.9, 7.12).
        slug_owners: dict[tuple[str, str], tuple[str, int]] = {}
        name_slugs: dict[tuple[str, str], tuple[str, int]] = {}

        for row in rows:
            try:
                rack_name = parse_rack_name(row.get("rack"))
                slugs = {
                    slug_column: row_slug(row, name_column, slug_column)
                    for name_column, slug_column in SLUGGED_LEVELS
                }
            except RowError:
                continue  # already reported in S2

            datacenter_slug = slugs["datacenter_slug"]
            datahall_slug = slugs["datahall_slug"]

            # Requirement 6.3: the rack name must agree with its own row's slugs.
            if not rack_name.agrees_with(
                datacenter_slug=datacenter_slug, datahall_slug=datahall_slug
            ):
                collector.add(
                    Severity.ERROR,
                    Code.RACK_NAME_DISAGREES_WITH_COLUMNS,
                    line=row.line,
                    column="rack",
                    value=rack_name.raw,
                    expected=build_rack_name(datacenter_slug, datahall_slug, rack_name.rack),
                    message=(
                        f"Rack name {rack_name.raw!r} says datacenter slug "
                        f"{rack_name.datacenter!r} and datahall slug {rack_name.datahall!r}, but "
                        f"this row's slugs are {datacenter_slug!r} and {datahall_slug!r}. One of "
                        f"them is wrong."
                    ),
                )

            # Requirements 7.12: one name cannot hold two slugs, and two names cannot share one.
            scopes = {
                "datacenter_slug": "site",
                "datahall_slug": f"location:{datacenter_slug.casefold()}",
            }
            for name_column, slug_column in SLUGGED_LEVELS:
                self._check_slug_agreement(
                    row=row,
                    collector=collector,
                    scope=scopes[slug_column],
                    name=row.get(name_column).strip(),
                    slug=slugs[slug_column],
                    name_column=name_column,
                    slug_column=slug_column,
                    slug_owners=slug_owners,
                    name_slugs=name_slugs,
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

            # The region's slug is always derived, so two differently spelled names that slug
            # identically would collide inside NetBox — an opaque 400 partway through the apply.
            value = row.get("location").strip()
            slug = derive_slug(value)
            if not slug:
                continue  # reported in S2 as an underivable slug
            owner = region_slugs.get(slug)
            if owner is None:
                region_slugs[slug] = (value, row.line)
            elif owner[0].casefold() != value.casefold():
                collector.add(
                    Severity.ERROR,
                    Code.SLUG_COLLISION,
                    line=row.line,
                    column="location",
                    value=value,
                    expected=f"a name whose slug differs from {owner[0]!r}",
                    message=(
                        f"{value!r} and {owner[0]!r} (line {owner[1]}) both reduce to the slug "
                        f"{slug!r}, so NetBox would treat them as one region. Rename one."
                    ),
                    related_line=owner[1],
                )

    def _check_slug_agreement(
        self,
        *,
        row: Row,
        collector: IssueCollector,
        scope: str,
        name: str,
        slug: str,
        name_column: str,
        slug_column: str,
        slug_owners: dict[tuple[str, str], tuple[str, int]],
        name_slugs: dict[tuple[str, str], tuple[str, int]],
    ) -> None:
        """Requirement 7.12, both directions, for one level of one row.

        A name given two slugs and a slug given two names are two spellings of the same mistake, and
        catching them here is what stops the apply from either failing on NetBox's slug uniqueness
        or quietly folding two rows into one object.
        """
        if not name:
            return
        folded_name, folded_slug = name.casefold(), slug.casefold()
        column = blame_column(row, name_column, slug_column)

        held = name_slugs.get((scope, folded_name))
        if held is None:
            name_slugs[(scope, folded_name)] = (slug, row.line)
        elif held[0].casefold() != folded_slug:
            collector.add(
                Severity.ERROR,
                Code.SLUG_INCONSISTENT_IN_FILE,
                line=row.line,
                column=column,
                value=slug,
                expected=held[0],
                message=(
                    f"{name!r} is given the slug {slug!r} here and {held[0]!r} on line {held[1]}. "
                    f"One object cannot hold two slugs — make them agree."
                ),
                related_line=held[1],
            )

        owner = slug_owners.get((scope, folded_slug))
        if owner is None:
            slug_owners[(scope, folded_slug)] = (name, row.line)
        elif owner[0].casefold() != folded_name:
            collector.add(
                Severity.ERROR,
                Code.SLUG_COLLISION,
                line=row.line,
                column=column,
                value=slug,
                expected=f"a slug other than {slug!r}",
                message=(
                    f"{name!r} and {owner[0]!r} (line {owner[1]}) are both given the slug "
                    f"{slug!r}, so NetBox would treat them as one object. Give one of them a "
                    f"different slug."
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

        # Lookups happen by slug as well as by name, so those candidates have to be loaded too
        # (Requirement 11.1) — by the *effective* slug for the two levels that have a column, so a
        # sheet that states a slug is looked up by the slug it states.
        region_slugs = {derive_slug(value) for value in regions if value}
        supplied: dict[str, set[str]] = {SITES: set(), LOCATIONS: set()}
        for row in rows:
            for endpoint, (name_column, slug_column) in zip(
                (SITES, LOCATIONS), SLUGGED_LEVELS, strict=True
            ):
                try:
                    supplied[endpoint].add(row_slug(row, name_column, slug_column))
                except RowError:
                    continue  # S2 reported it; S4 would never be reached with it unresolved

        for endpoint, values in (
            (REGIONS, region_slugs),
            (SITES, supplied[SITES]),
            (LOCATIONS, supplied[LOCATIONS]),
        ):
            slugs = sorted(values - {""})
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
            # Resolved once and passed down, so the lookup, the payload, and the rack name are
            # guaranteed to be talking about the same slug (Requirement 7.10).
            site_slug = row_slug(row, "datacenter", "datacenter_slug")
            hall_slug = row_slug(row, "datahall", "datahall_slug")
        except RowError as error:
            collector.add_row_error(error, row.line)
            planned.outcome = RowOutcome.BLOCKED
            return planned

        region = self._region(row, lookups, registry, state, collector, planned)
        site = self._site(row, region, site_slug, lookups, registry, state, collector, planned)
        hall = self._location(row, site, hall_slug, lookups, registry, state, collector, planned)
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
        slug: str,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> Resolution:
        name = row.get("datacenter").strip()
        resolution = Resolution(endpoint=SITES, key=Registry.key(slug), label=f"Site {name}")

        existing = lookups.get_or_none(SITES, name=name) or lookups.get_or_none(SITES, slug=slug)
        if existing is not None:
            if not self._slug_matches(existing, name, slug, "datacenter", row, collector, "site"):
                planned.outcome = RowOutcome.BLOCKED
                return resolution

            if not self._supplied_slug_matches(
                existing, "datacenter_slug", row, lookups, SITES, collector, "Site"
            ):
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
        slug: str,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> Resolution:
        name = row.get("datahall").strip()
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
            # Both checks are scoped to the site by the lookup above, which is what Requirement 7.9
            # means for slugs: the same datahall slug under a second datacenter is a different room.
            if not self._slug_matches(existing, name, slug, "datahall", row, collector, "datahall"):
                planned.outcome = RowOutcome.BLOCKED
                return resolution

            if not self._supplied_slug_matches(
                existing, "datahall_slug", row, lookups, LOCATIONS, collector, "Datahall"
            ):
                planned.outcome = RowOutcome.BLOCKED
                return resolution

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
        """Requirement 7.3: this row's slug already belongs to a *differently named* object.

        The other direction — this object exists and its slug is not the one the sheet asserted — is
        `_supplied_slug_matches` below. Two questions, two answers: one says "that slug is taken",
        the other says "that object does not have the slug you gave".
        """
        if str(existing.get("name", "")).strip().casefold() == name.strip().casefold():
            return True
        if str(existing.get("slug", "")).casefold() != slug.casefold():
            return True
        collector.add(
            Severity.ERROR,
            Code.SLUG_COLLISION,
            line=row.line,
            column=column,
            value=name,
            expected=f"a name or slug other than {slug!r}",
            message=(
                f"The slug for {name!r} is {slug!r}, which already belongs to the "
                f"{kind} {existing.get('name')!r}. NetBox requires slugs to be unique, so this "
                f"row would either fail or attach to the wrong object. Rename one of them, or give "
                f"this one its own slug."
            ),
            related_object=f"{kind} {existing.get('name')}",
        )
        return False

    def _supplied_slug_matches(
        self,
        existing: dict[str, Any],
        slug_column: str,
        row: Row,
        lookups: Lookups,
        endpoint: str,
        collector: IssueCollector,
        kind: str,
    ) -> bool:
        """Requirement 7.13: a slug the sheet *stated* must be the one the matched object holds.

        Only a supplied slug is compared. A derived one is the tool's guess, not the operator's
        assertion, and an object created outside this tool has no reason to match it — blocking on
        that would reject rows that pass today (Requirement 7.14). `compare_to_existing` enforces
        the distinction for free by returning nothing for a blank cell, and the column's
        CONSEQUENTIAL classification is what makes the difference an error rather than a warning.
        """
        conflict = compare_to_existing(
            self.specs()[slug_column],
            row.get(slug_column),
            existing.get("slug"),
            object_label=f"{kind} {existing.get('name')}",
            related_url=self._url(lookups, endpoint, existing),
        )
        if conflict is None:
            return True
        collector.add_row_error(conflict, row.line)
        return conflict.severity is not Severity.ERROR

    @staticmethod
    def _url(lookups: Lookups, endpoint: str, existing: dict[str, Any]) -> str:
        object_id = existing.get("id")
        return lookups.client.object_url(endpoint, int(object_id)) if object_id else ""
