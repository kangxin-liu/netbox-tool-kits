# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Cached natural-key lookups, so validating 2,000 rows is a handful of requests (Req 15.2).

Two behaviours of the NetBox API shape this module, both confirmed against 4.6.8:

1. **Unrecognised query parameters are silently ignored.** `?bogus__lookup=x` returns *every*
   object with HTTP 200 rather than rejecting the request. A mistyped filter name would therefore
   look like a successful match against the wrong records. Server-side filters are treated as a
   bandwidth hint only: every record is re-verified locally against the natural key before it is
   accepted as a match.
2. **Filtering by an id that no longer exists returns 400**, not an empty list. That matters for the
   pre-apply re-check, where an object may have been deleted since validation, so an id filter
   rejected this way is read as "not found" rather than allowed to abort the run.

Lookups are case-insensitive (Requirement 6.4, 11.1) because the index is keyed on case-folded
values, while the record keeps whatever casing NetBox holds.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from importer.services.netbox_client import Failure, NetBoxClient, NetBoxUnavailable

# Natural keys per endpoint (Requirement 11.1). Each tuple is one way to find the object; a field
# naming a related object is matched on that object's id.
NATURAL_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    "dcim.regions": (("slug",), ("name",)),
    "dcim.sites": (("slug",), ("name",)),
    "dcim.locations": (
        ("site", "parent", "name"),
        ("site", "parent", "slug"),
        ("site", "slug"),
        ("site", "name"),
    ),
    "dcim.racks": (("site", "name"), ("name",)),
    "dcim.device-types": (("model",), ("slug",)),
    "dcim.device-roles": (("slug",), ("name",)),
    "dcim.manufacturers": (("slug",), ("name",)),
    "dcim.devices": (("name",),),
    "dcim.interface-templates": (("device_type", "name"),),
    "dcim.interfaces": (("device", "name"),),
    "ipam.ip-addresses": (("address",),),
    "tenancy.tenants": (("slug",), ("name",)),
    "extras.tags": (("slug",), ("name",)),
}

TerminationKind = Literal["interface", "frontport", "rearport"]

# NetBox object_type string → the endpoint its records live on.
TERMINATION_ENDPOINTS: dict[str, str] = {
    "interface": "dcim.interfaces",
    "frontport": "dcim.front-ports",
    "rearport": "dcim.rear-ports",
}

# Chunk multi-value filters so a sheet naming 2,000 devices does not build one enormous URL.
CHUNK_SIZE = 100


def fold(value: Any) -> str:
    return str(value).strip().casefold()


def key_value(record: dict[str, Any], field_name: str) -> Any:
    """The comparable value of one natural-key field.

    A related object is compared on its id; everything else on its case-folded text.
    """
    raw = record.get(field_name)
    if isinstance(raw, dict):
        return raw.get("id")
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    return fold(raw)


@dataclass
class Lookups:
    """One instance per validation run or per request. Not shared between users."""

    client: NetBoxClient
    _records: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _index: dict[str, dict[tuple[str, ...], dict[tuple, dict[str, Any]]]] = field(
        default_factory=dict
    )
    _terminations: dict[str, dict[int, dict[str, dict[str, Any]]]] = field(default_factory=dict)
    _terminations_by_id: dict[str, dict[int, dict[str, Any]]] = field(default_factory=dict)

    # --- generic object lookups -------------------------------------------------------------

    def prefetch(self, endpoint: str, *, names: list[str] | None = None, **filters: Any) -> None:
        """Load a set of records into the cache in as few requests as the filters allow.

        Calling this twice for the same endpoint merges the results, so an importer can prefetch by
        name and later by parent scope without losing either.
        """
        if names is not None:
            unique = sorted({name for name in names if str(name).strip()})
            for start in range(0, len(unique), CHUNK_SIZE):
                chunk = unique[start : start + CHUNK_SIZE]
                self._load(endpoint, {**filters, "name__ie": chunk})
            if not unique:
                self._records.setdefault(endpoint, [])
            return
        for field_name, values in filters.items():
            if not isinstance(values, (list, tuple, set, frozenset)):
                continue
            unique = sorted({value for value in values if str(value).strip()})
            for start in range(0, len(unique), CHUNK_SIZE):
                chunk = unique[start : start + CHUNK_SIZE]
                self._load(endpoint, {**filters, field_name: chunk})
            if not unique:
                self._records.setdefault(endpoint, [])
            return
        self._load(endpoint, filters)

    def _load(self, endpoint: str, filters: dict[str, Any]) -> None:
        try:
            fetched = self.client.get_all(endpoint, **filters)
        except NetBoxUnavailable as unavailable:
            # A filter naming an id that has since been deleted comes back as 400. Treated as
            # "nothing matches" so a deleted object surfaces as a missing reference rather than an
            # unexplained abort.
            if unavailable.category is Failure.SERVER:
                fetched = []
            else:
                raise
        self.add_records(endpoint, fetched)

    def add_records(self, endpoint: str, records: list[dict[str, Any]]) -> None:
        """Index records under every natural key defined for the endpoint."""
        known = self._records.setdefault(endpoint, [])
        seen_ids = {record.get("id") for record in known}
        for record in records:
            if record.get("id") not in seen_ids:
                known.append(record)
                seen_ids.add(record.get("id"))

        indexes = self._index.setdefault(endpoint, {})
        for fields in NATURAL_KEYS.get(endpoint, (("name",),)):
            bucket = indexes.setdefault(fields, {})
            for record in records:
                composite = tuple(key_value(record, name) for name in fields)
                if all(part is not None for part in composite):
                    bucket.setdefault(composite, record)

    def get_or_none(self, endpoint: str, **natural_key: Any) -> dict[str, Any] | None:
        """Find one record by natural key, from cache when possible.

        Falls back to a single targeted request, so a lookup an importer forgot to prefetch is
        correct but slower rather than wrong.
        """
        fields = tuple(natural_key)
        wanted = tuple(_comparable(value) for value in natural_key.values())

        bucket = self._index.get(endpoint, {}).get(fields)
        if bucket is not None and wanted in bucket:
            return bucket[wanted]

        if endpoint in self._records and bucket is not None:
            # The endpoint has been prefetched under this key and the value is absent: that is a
            # definitive miss, not a reason for another request.
            return None

        self._load(endpoint, _server_filters(natural_key))
        bucket = self._index.get(endpoint, {}).get(fields, {})
        return bucket.get(wanted)

    def get_matches(self, endpoint: str, **natural_key: Any) -> list[dict[str, Any]]:
        """Return every locally verified match for a natural key.

        NetBox permits duplicate display names in several models. Importers resolving prepared
        prerequisites use this method so ambiguity blocks instead of silently selecting the first
        record added to the cache.
        """
        fields = tuple(natural_key)
        wanted = tuple(_comparable(value) for value in natural_key.values())
        bucket = self._index.get(endpoint, {}).get(fields)
        if endpoint not in self._records or bucket is None:
            self._load(endpoint, _server_filters(natural_key))
        return [
            record
            for record in self._records.get(endpoint, [])
            if tuple(key_value(record, field_name) for field_name in fields) == wanted
        ]

    def all_records(self, endpoint: str) -> list[dict[str, Any]]:
        return list(self._records.get(endpoint, []))

    def count(self, endpoint: str) -> int:
        return len(self._records.get(endpoint, []))

    def choices_for(self, endpoint: str, field_name: str) -> frozenset[str]:
        return self.client.choices_for(endpoint, field_name)

    # --- terminations -----------------------------------------------------------------------

    def prefetch_terminations(self, device_ids: list[int], kinds: tuple[str, ...] = ()) -> None:
        """Load the ports of every named device in bulk, for cabling validation."""
        unique = sorted({int(device_id) for device_id in device_ids})
        for kind in kinds or tuple(TERMINATION_ENDPOINTS):
            endpoint = TERMINATION_ENDPOINTS[kind]
            by_device = self._terminations.setdefault(kind, {})
            by_id = self._terminations_by_id.setdefault(kind, {})
            for device_id in unique:
                by_device.setdefault(device_id, {})
            for start in range(0, len(unique), CHUNK_SIZE):
                chunk = unique[start : start + CHUNK_SIZE]
                for record in self._fetch_terminations(endpoint, chunk):
                    device = record.get("device") or {}
                    device_id = device.get("id")
                    if device_id is None:
                        continue
                    by_device.setdefault(int(device_id), {})[fold(record.get("name"))] = record
                    by_id[int(record["id"])] = record

    def _fetch_terminations(self, endpoint: str, device_ids: list[int]) -> list[dict[str, Any]]:
        try:
            return self.client.get_all(endpoint, device_id=device_ids)
        except NetBoxUnavailable as unavailable:
            if unavailable.category is Failure.SERVER:
                return []
            raise

    def terminations_for(self, device_id: int, kind: TerminationKind) -> dict[str, dict[str, Any]]:
        """Port name (case-folded) → the port record, for one device and one port model."""
        cached = self._terminations.get(kind, {}).get(int(device_id))
        if cached is None:
            self.prefetch_terminations([int(device_id)], (kind,))
            cached = self._terminations.get(kind, {}).get(int(device_id), {})
        return cached

    def occupied(self, object_type: str, object_id: int) -> dict[str, Any] | None:
        """The cable already attached to a termination, if any (Requirement 9.9).

        Read from the termination record the ports prefetch already loaded, so checking occupancy
        costs no extra request.
        """
        kind = object_type.split(".")[-1]
        record = self._terminations_by_id.get(kind, {}).get(int(object_id))
        if record is None:
            return None
        cable = record.get("cable")
        if not cable:
            return None
        return cable if isinstance(cable, dict) else {"id": cable}


def _comparable(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("id")
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    return fold(value)


def _server_filters(natural_key: dict[str, Any]) -> dict[str, Any]:
    """Turn a natural key into filters for one targeted request.

    Case-insensitive lookups (`__ie`) are used for text so the request matches what the local index
    would. The result is still re-verified locally, because an unrecognised parameter name would
    otherwise return every object with HTTP 200.
    """
    filters: dict[str, Any] = {}
    for name, value in natural_key.items():
        if isinstance(value, dict):
            filters[f"{name}_id"] = value.get("id")
        elif isinstance(value, int):
            filters[f"{name}_id"] = value
        else:
            filters[f"{name}__ie"] = str(value).strip()
    return filters
