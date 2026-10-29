# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Sheet 1 — create direct child locations and racks below prepared NetBox parents (D16).

The site and parent location are prerequisites. This importer never creates, moves, or updates
either one. A row resolves the site by ``site_slug`` and the parent location by
``(site, parent_location_slug)``, then creates or reuses one direct child location and one rack.

Rack names are opaque natural keys. Hyphens and underscores are ordinary characters; hierarchy is
enforced exclusively through NetBox's site, parent, and location relationships.
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
from importer.naming import RackName, derive_slug, validate_rack_name, validate_slug
from importer.services.lookups import Lookups
from importer.services.sheets import Row
from importer.validation import (
    Code,
    IssueCollector,
    RowError,
    Severity,
    compare_to_existing,
)

SITES = "dcim.sites"
LOCATIONS = "dcim.locations"
RACKS = "dcim.racks"
TENANTS = "tenancy.tenants"

DEFAULT_WIDTH = 19
DEFAULT_STATUS = "active"


@dataclass
class Resolution:
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
        return int(self.existing["id"]) if self.existing is not None else None


class HierarchyImporter(BaseImporter):
    name = "hierarchy"
    endpoint_order = (LOCATIONS, RACKS)

    choice_sources: ClassVar[dict[str, tuple[str, str]]] = {
        "rack_status": (RACKS, "status"),
        "rack_width": (RACKS, "width"),
        "rack_form_factor": (RACKS, "form_factor"),
    }

    # --- S2 ---------------------------------------------------------------------------------

    def extra_cell_checks(self, row: Row, lookups: Lookups) -> list[RowError]:
        errors: list[RowError] = []
        try:
            validate_rack_name(row.get("rack"))
        except RowError as error:
            errors.append(error)
        for column in ("site_slug", "parent_location_slug", "datahall_slug"):
            if not row.get(column).strip():
                continue
            try:
                validate_slug(row.get(column), column=column)
            except RowError as error:
                errors.append(error)
        height = row.get("rack_u_height").strip()
        if height.isdigit() and int(height) > 100:
            errors.append(
                RowError(
                    Code.NOT_A_POSITIVE_INTEGER,
                    column="rack_u_height",
                    value=height,
                    expected="a whole number from 1 to 100",
                    message=f"rack_u_height must be within NetBox's 1–100 range, not {height}.",
                )
            )
        return errors

    # --- S3 ---------------------------------------------------------------------------------

    def check_consistency(self, rows: list[Row], collector: IssueCollector) -> None:
        seen_racks: dict[str, int] = {}
        names_to_slugs: dict[tuple[str, str, str], tuple[str, int]] = {}
        slugs_to_names: dict[tuple[str, str, str], tuple[str, int]] = {}

        for row in rows:
            try:
                rack = validate_rack_name(row.get("rack"))
            except RowError:
                continue

            previous = seen_racks.get(rack.key)
            if previous is not None:
                collector.add(
                    Severity.ERROR,
                    Code.DUPLICATE_RACK_IN_FILE,
                    line=row.line,
                    column="rack",
                    value=rack.raw,
                    expected="one row per rack",
                    message=(
                        f"Rack {rack.raw!r} is already declared on line {previous}. "
                        "Remove the duplicate row."
                    ),
                    related_line=previous,
                )
            else:
                seen_racks[rack.key] = row.line

            scope = (
                row.get("site_slug").strip().casefold(),
                row.get("parent_location_slug").strip().casefold(),
            )
            name = row.get("datahall").strip()
            slug = row.get("datahall_slug").strip()
            name_key = (*scope, name.casefold())
            slug_key = (*scope, slug.casefold())

            held_slug = names_to_slugs.get(name_key)
            if held_slug is None:
                names_to_slugs[name_key] = (slug, row.line)
            elif held_slug[0].casefold() != slug.casefold():
                collector.add(
                    Severity.ERROR,
                    Code.SLUG_INCONSISTENT_IN_FILE,
                    line=row.line,
                    column="datahall_slug",
                    value=slug,
                    expected=held_slug[0],
                    message=(
                        f"Child location {name!r} uses slug {slug!r} here and "
                        f"{held_slug[0]!r} on line {held_slug[1]}. Make them agree."
                    ),
                    related_line=held_slug[1],
                )

            held_name = slugs_to_names.get(slug_key)
            if held_name is None:
                slugs_to_names[slug_key] = (name, row.line)
            elif held_name[0].casefold() != name.casefold():
                collector.add(
                    Severity.ERROR,
                    Code.SLUG_COLLISION,
                    line=row.line,
                    column="datahall_slug",
                    value=slug,
                    expected=f"a slug other than {slug!r}",
                    message=(
                        f"Child locations {name!r} and {held_name[0]!r} (line "
                        f"{held_name[1]}) share slug {slug!r} under the same parent."
                    ),
                    related_line=held_name[1],
                )

    # --- S4 ---------------------------------------------------------------------------------

    def prefetch(self, rows: list[Row], lookups: Lookups) -> None:
        site_slugs = sorted({row.get("site_slug").strip() for row in rows})
        parent_slugs = sorted({row.get("parent_location_slug").strip() for row in rows})
        child_slugs = sorted({row.get("datahall_slug").strip() for row in rows})
        child_names = [row.get("datahall") for row in rows]
        rack_names = [row.get("rack") for row in rows]
        tenant_names = [row.get("tenant") for row in rows if row.get("tenant")]

        lookups.prefetch(SITES, slug__ie=site_slugs)
        lookups.prefetch(LOCATIONS, slug__ie=sorted(set(parent_slugs + child_slugs)))
        lookups.prefetch(LOCATIONS, names=child_names)
        lookups.prefetch(RACKS, names=rack_names)
        if tenant_names:
            lookups.prefetch(TENANTS, names=tenant_names)

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
            rack_name = validate_rack_name(row.get("rack"))
            site_slug = validate_slug(row.get("site_slug"), column="site_slug")
            parent_slug = validate_slug(
                row.get("parent_location_slug"), column="parent_location_slug"
            )
            child_slug = validate_slug(row.get("datahall_slug"), column="datahall_slug")
        except RowError as error:
            collector.add_row_error(error, row.line)
            planned.outcome = RowOutcome.BLOCKED
            return planned

        site = self._site(row, site_slug, lookups, registry, state, collector, planned)
        if site is None:
            return planned
        parent = self._parent(row, site, parent_slug, lookups, registry, state, collector, planned)
        if parent is None:
            return planned
        child = self._child(
            row,
            site,
            parent,
            child_slug,
            lookups,
            registry,
            state,
            collector,
            planned,
        )
        if child is None:
            return planned
        rack = self._rack(
            row,
            rack_name,
            site,
            child,
            lookups,
            registry,
            state,
            collector,
            planned,
        )

        for resolution in (child, rack):
            if resolution.action is not None:
                planned.actions.append(resolution.action)

        if planned.outcome != RowOutcome.BLOCKED:
            planned.outcome = RowOutcome.CREATE if planned.actions else RowOutcome.SKIP_EXISTS
            if not planned.actions:
                collector.add(
                    Severity.INFO,
                    Code.ALREADY_EXISTS,
                    line=row.line,
                    column="rack",
                    value=rack_name.raw,
                    message=(
                        f"Rack {rack_name.raw!r} already exists in the requested child location. "
                        "Nothing will be created."
                    ),
                    related_object=rack.label,
                )
        return planned

    # --- prepared prerequisites --------------------------------------------------------------

    def _site(
        self,
        row: Row,
        slug: str,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> Resolution | None:
        matches = lookups.get_matches(SITES, slug=slug)
        if len(matches) != 1:
            qualifier = "No" if not matches else f"{len(matches)}"
            collector.add(
                Severity.ERROR,
                (
                    Code.SITE_PREREQUISITE_MISSING
                    if not matches
                    else Code.SITE_PREREQUISITE_AMBIGUOUS
                ),
                line=row.line,
                column="site_slug",
                value=slug,
                expected="the unique slug of an existing NetBox site",
                message=(
                    f"{qualifier} NetBox site(s) match slug {slug!r}. Create or correct the site "
                    "before importing hierarchy rows; this importer never creates sites."
                ),
                related_object=(f"Site {matches[0].get('name')}" if matches else None),
                related_url=(self._url(lookups, SITES, matches[0]) if matches else None),
            )
            planned.outcome = RowOutcome.BLOCKED
            return None

        existing = matches[0]
        resolution = Resolution(
            endpoint=SITES,
            key=Registry.key(slug),
            label=f"Site {existing.get('name')}",
            existing=existing,
        )
        self._register(
            state,
            registry,
            resolution,
            f"id={existing['id']},slug={existing.get('slug')}",
        )
        planned.resolved["site_slug"] = f"{existing.get('name')} (existing)"
        return resolution

    def _parent(
        self,
        row: Row,
        site: Resolution,
        slug: str,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> Resolution | None:
        site_id = site.object_id
        matches = lookups.get_matches(LOCATIONS, site=site_id, slug=slug)
        if len(matches) != 1:
            elsewhere = lookups.get_matches(LOCATIONS, slug=slug)
            if not matches and elsewhere:
                detail = "exists, but under a different site"
                code = Code.PARENT_LOCATION_WRONG_SITE
            elif len(matches) > 1:
                detail = f"is ambiguous ({len(matches)} matches in the site)"
                code = Code.PARENT_LOCATION_AMBIGUOUS
            else:
                detail = "does not exist in the site"
                code = Code.PARENT_LOCATION_MISSING
            related = (elsewhere or matches)[0] if (elsewhere or matches) else None
            collector.add(
                Severity.ERROR,
                code,
                line=row.line,
                column="parent_location_slug",
                value=slug,
                expected="one existing location in the selected site",
                message=(
                    f"Parent location slug {slug!r} {detail}. Create or correct the parent "
                    "location before importing; this importer never creates or moves it."
                ),
                related_object=(f"Location {related.get('name')}" if related is not None else None),
                related_url=(
                    self._url(lookups, LOCATIONS, related) if related is not None else None
                ),
            )
            planned.outcome = RowOutcome.BLOCKED
            return None

        existing = matches[0]
        resolution = Resolution(
            endpoint=LOCATIONS,
            key=Registry.key(site.key[0], slug),
            label=f"Parent location {existing.get('name')}",
            existing=existing,
        )
        self._register(
            state,
            registry,
            resolution,
            f"id={existing['id']},site={_related_id(existing, 'site')},"
            f"parent={_related_id(existing, 'parent')}",
        )
        planned.resolved["parent_location_slug"] = f"{existing.get('name')} (existing)"
        return resolution

    # --- objects this importer owns ----------------------------------------------------------

    def _child(
        self,
        row: Row,
        site: Resolution,
        parent: Resolution,
        slug: str,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> Resolution | None:
        name = row.get("datahall").strip()
        site_id = site.object_id
        parent_id = parent.object_id
        resolution = Resolution(
            endpoint=LOCATIONS,
            key=Registry.key(site.key[0], parent.key[-1], slug),
            label=f"Child location {name}",
        )

        by_name = lookups.get_matches(LOCATIONS, site=site_id, parent=parent_id, name=name)
        by_slug = lookups.get_matches(LOCATIONS, site=site_id, parent=parent_id, slug=slug)
        if len(by_name) > 1 or len(by_slug) > 1:
            collector.add(
                Severity.ERROR,
                Code.CHILD_LOCATION_CONFLICT,
                line=row.line,
                column="datahall",
                value=name,
                expected="one direct child matching the supplied name and slug",
                message=(
                    f"Child location {name!r}/{slug!r} is ambiguous under the selected parent. "
                    "Resolve the duplicate NetBox records before importing."
                ),
            )
            planned.outcome = RowOutcome.BLOCKED
            return None

        named = by_name[0] if by_name else None
        slugged = by_slug[0] if by_slug else None
        if named is not None and slugged is not None and named.get("id") != slugged.get("id"):
            collector.add(
                Severity.ERROR,
                Code.CHILD_LOCATION_CONFLICT,
                line=row.line,
                column="datahall_slug",
                value=slug,
                expected=f"the slug of child location {name!r}",
                message=(
                    f"Name {name!r} and slug {slug!r} resolve to different child locations "
                    "under the selected parent. No record will be changed."
                ),
            )
            planned.outcome = RowOutcome.BLOCKED
            return None

        existing = named or slugged
        if existing is None:
            wrong_parent_name = lookups.get_matches(LOCATIONS, site=site_id, name=name)
            wrong_parent_slug = lookups.get_matches(LOCATIONS, site=site_id, slug=slug)
            if wrong_parent_name or wrong_parent_slug:
                held = (wrong_parent_name or wrong_parent_slug)[0]
                collector.add(
                    Severity.ERROR,
                    Code.CHILD_LOCATION_CONFLICT,
                    line=row.line,
                    column="parent_location_slug",
                    value=row.get("parent_location_slug").strip(),
                    expected="the existing child location's current parent",
                    message=(
                        f"Location {held.get('name')!r}/{held.get('slug')!r} already exists in "
                        "this site under a different parent. Existing locations are never moved."
                    ),
                    related_object=f"Location {held.get('name')}",
                    related_url=self._url(lookups, LOCATIONS, held),
                )
                planned.outcome = RowOutcome.BLOCKED
                return None

            resolution.action = CreateAction(
                endpoint=LOCATIONS,
                payload={
                    "name": name,
                    "slug": slug,
                    "site": site_id,
                    "parent": parent_id,
                },
                key=resolution.key,
                label=f"Child location {name}",
                line=row.line,
                object_kind="location",
            )
            planned.resolved["datahall"] = f"{name} (create)"
            return resolution

        if (
            str(existing.get("name", "")).strip().casefold() != name.casefold()
            or str(existing.get("slug", "")).strip().casefold() != slug.casefold()
            or _related_id(existing, "site") != site_id
            or _related_id(existing, "parent") != parent_id
        ):
            collector.add(
                Severity.ERROR,
                Code.CHILD_LOCATION_CONFLICT,
                line=row.line,
                column="datahall_slug",
                value=slug,
                expected=str(existing.get("slug", "")),
                message=(
                    f"Existing location {existing.get('name')!r} does not exactly match the "
                    "requested child name, slug, site, and parent. Existing locations are never "
                    "renamed or moved."
                ),
                related_object=f"Location {existing.get('name')}",
                related_url=self._url(lookups, LOCATIONS, existing),
            )
            planned.outcome = RowOutcome.BLOCKED
            return None

        resolution.existing = existing
        self._register(
            state,
            registry,
            resolution,
            f"id={existing['id']},site={_related_id(existing, 'site')},"
            f"parent={_related_id(existing, 'parent')},slug={existing.get('slug')}",
        )
        planned.resolved["datahall"] = f"{existing.get('name')} (existing)"
        return resolution

    def _rack(
        self,
        row: Row,
        rack_name: RackName,
        site: Resolution,
        child: Resolution,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> Resolution:
        resolution = Resolution(
            endpoint=RACKS,
            key=Registry.key(rack_name.raw),
            label=f"Rack {rack_name.raw}",
        )
        tenant_id = self._tenant(row, lookups, collector, planned)
        if tenant_id is False:
            planned.outcome = RowOutcome.BLOCKED
            return resolution

        matches = lookups.get_matches(RACKS, name=rack_name.raw)
        if len(matches) > 1:
            candidates = ", ".join(
                f"{(match.get('site') or {}).get('name') or '(no site)'}/"
                f"{(match.get('location') or {}).get('name') or '(no location)'} "
                f"(id {match.get('id')})"
                for match in matches
            )
            collector.add(
                Severity.ERROR,
                Code.RACK_AMBIGUOUS,
                line=row.line,
                column="rack",
                value=rack_name.raw,
                expected="one globally unique rack name",
                message=(
                    f"Rack name {rack_name.raw!r} is ambiguous ({len(matches)} matches). "
                    f"Candidates: {candidates}. Resolve the duplicate NetBox records before "
                    "importing."
                ),
            )
            planned.outcome = RowOutcome.BLOCKED
            return resolution

        existing = matches[0] if matches else None
        if existing is not None:
            resolution.existing = existing
            expected_location = child.object_id
            relation_matches = (
                _related_id(existing, "site") == site.object_id
                and expected_location is not None
                and _related_id(existing, "location") == expected_location
            )
            if not relation_matches:
                collector.add(
                    Severity.ERROR,
                    Code.EXISTS_BUT_DIFFERS,
                    line=row.line,
                    column="rack",
                    value=rack_name.raw,
                    expected="a rack in the selected site and direct child location",
                    message=(
                        f"Rack {rack_name.raw!r} already exists under a different site or "
                        "location. Existing racks are never moved."
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
                    self.specs()[key],
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
                f"site={_related_id(existing, 'site')},"
                f"location={_related_id(existing, 'location')}",
            )
            planned.resolved["rack"] = f"{existing.get('name')} (existing)"
            return resolution

        payload: dict[str, Any] = {
            "name": rack_name.raw,
            "site": site.object_id,
            "location": child.ref,
            "u_height": int(row.get("rack_u_height")),
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

    # --- shared helpers ---------------------------------------------------------------------

    def _tenant(
        self,
        row: Row,
        lookups: Lookups,
        collector: IssueCollector,
        planned: PlannedRow,
    ) -> int | None | bool:
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
                    "tenants: create it first, or clear the column."
                ),
            )
            return False
        planned.resolved["tenant"] = f"{tenant.get('name')} (existing)"
        return int(tenant["id"])

    def _register(
        self,
        state: dict[str, str],
        registry: Registry,
        resolution: Resolution,
        signature: str,
    ) -> None:
        if resolution.existing is None:
            return
        registry.remember(resolution.endpoint, resolution.key, int(resolution.existing["id"]))
        state[f"{resolution.endpoint}:{'/'.join(str(part) for part in resolution.key)}"] = signature

    @staticmethod
    def _url(lookups: Lookups, endpoint: str, existing: dict[str, Any]) -> str:
        object_id = existing.get("id")
        return lookups.client.object_url(endpoint, int(object_id)) if object_id else ""


def _related_id(record: dict[str, Any], field_name: str) -> int | None:
    related = record.get(field_name)
    if isinstance(related, dict):
        value = related.get("id")
        return int(value) if value is not None else None
    return int(related) if related is not None else None
