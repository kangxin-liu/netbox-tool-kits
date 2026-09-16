# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""An in-memory stand-in for NetBox, for testing importers rather than HTTP.

`test_netbox_client.py` covers the real transport with stubbed HTTP. Above that layer the
interesting behaviour is what the importers *decide*, so this double implements the client's
surface and records what was posted. It deliberately reproduces two real behaviours of NetBox 4.6.8:

* an unrecognised query parameter is ignored rather than rejected, so a filter this fake does not
  understand returns every record — the same trap the lookup cache guards against;
* a list POST is atomic: if the batch is rejected, nothing in it is stored;
* an endpoint's choices are fetched once and then cached, as `NetBoxClient` caches them, so a
  request-budget assertion measures the importer rather than the double;
* creating a cable **occupies its terminations**: NetBox writes the new cable's id onto each
  terminated interface, front port, and rear port. Without that, re-uploading a cabling sheet would
  look like it created duplicates here while being correctly skipped against a real NetBox, so the
  double would hide the bug it exists to expose (Requirements 9.9, 11.7).

All data is synthetic: example names, no real hosts, no credentials.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from importer.cable_profiles import PROFILES as CABLE_PROFILES
from importer.services.netbox_client import NetBoxStatus, NetBoxWriteRejected

# Enough of NetBox's OPTIONS choices for the sheets to validate against, spelled as NetBox spells
# them (Requirement 7.7 reads these from the API in production).
CHOICES: dict[str, dict[str, frozenset[str]]] = {
    "dcim.racks": {
        "status": frozenset({"reserved", "available", "planned", "active", "deprecated"}),
        "width": frozenset({"10", "19", "21", "23"}),
        "form_factor": frozenset(
            {
                "2-post-frame",
                "4-post-frame",
                "4-post-cabinet",
                "wall-frame",
                "wall-frame-vertical",
                "wall-cabinet",
                "wall-cabinet-vertical",
            }
        ),
    },
    "dcim.devices": {
        "status": frozenset(
            {"offline", "active", "planned", "staged", "failed", "inventory", "decommissioning"}
        ),
        "face": frozenset({"front", "rear"}),
    },
    "dcim.interfaces": {
        # What `*_interface_type` validates against (Requirement 9.27). A slice, as the real
        # OPTIONS response is ~180 values; `wet-string` is absent from it, which is the point.
        "type": frozenset(
            {
                "virtual",
                "1000base-t",
                "10gbase-t",
                "10gbase-x-sfpp",
                "25gbase-x-sfp28",
                "40gbase-x-qsfpp",
                "100gbase-x-qsfp28",
                "200gbase-x-qsfp56",
                "400gbase-x-qsfpdd",
                "800gbase-x-osfp",
            }
        ),
    },
    "dcim.cables": {
        "type": frozenset(
            {
                "cat6",
                "cat6a",
                "mmf-om3",
                "mmf-om4",
                "smf-os2",
                "dac-passive",
                "aoc",
                "power",
            }
        ),
        "status": frozenset({"connected", "planned", "decommissioning"}),
        "length_unit": frozenset({"km", "m", "cm", "mi", "ft", "in"}),
        # The whole catalogue NetBox 4.6.8 reports, from the same constant the templates use. An
        # earlier version of this double carried a representative slice instead, on the grounds
        # that validation reads whatever the API returns — which was true and still made a
        # perfectly ordinary profile like `trunk-4c4p-shuffle` fail against the double alone.
        "profile": frozenset(CABLE_PROFILES),
    },
}


DEVICES = "dcim.devices"
INTERFACES = "dcim.interfaces"
INTERFACE_TEMPLATES = "dcim.interface-templates"
IP_ADDRESSES = "ipam.ip-addresses"
CABLES = "dcim.cables"

# A cable's terminations name their model, not their endpoint (Requirement 9.6).
TERMINATION_ENDPOINTS: dict[str, str] = {
    "dcim.interface": "dcim.interfaces",
    "dcim.frontport": "dcim.front-ports",
    "dcim.rearport": "dcim.rear-ports",
}


@dataclass
class Post:
    endpoint: str
    payloads: list[dict[str, Any]]

    @property
    def size(self) -> int:
        return len(self.payloads)


@dataclass
class FakeNetBox:
    """The `NetBoxClient` surface, in memory."""

    objects: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    version: str = "4.6.8"
    host: str = "netbox.example.com"
    posts: list[Post] = field(default_factory=list)
    patches: list[Post] = field(default_factory=list)
    rejections: dict[str, Callable[[list[dict[str, Any]]], Any]] = field(default_factory=dict)
    unavailable_on: dict[str, Exception] = field(default_factory=dict)
    """Endpoint → transport failure raised on *writes*, for mid-apply abort tests."""
    unavailable_on_read: dict[str, Exception] = field(default_factory=dict)
    request_count: int = 0
    _next_id: int = 1000
    _choices_fetched: set[str] = field(default_factory=set)

    # --- test-side helpers ------------------------------------------------------------------

    def add(self, endpoint: str, **fields: Any) -> dict[str, Any]:
        record = {"id": self._take_id(), **fields}
        self.objects.setdefault(endpoint, []).append(record)
        return record

    def reject(self, endpoint: str, decide: Callable[[list[dict[str, Any]]], Any]) -> None:
        """`decide(payloads)` returns None to accept, or `(status, body)` to reject."""
        self.rejections[endpoint] = decide

    def stored(self, endpoint: str) -> list[dict[str, Any]]:
        return list(self.objects.get(endpoint, []))

    def posted(self, endpoint: str) -> list[Post]:
        return [post for post in self.posts if post.endpoint == endpoint]

    def _take_id(self) -> int:
        self._next_id += 1
        return self._next_id

    # --- the client surface -----------------------------------------------------------------

    def status(self, *, refresh: bool = False) -> NetBoxStatus:
        return NetBoxStatus(version=self.version, host=self.host, supported=True)

    def get_all(self, endpoint: str, **filters: Any) -> list[dict[str, Any]]:
        self.request_count += 1
        if endpoint in self.unavailable_on_read:
            raise self.unavailable_on_read[endpoint]
        records = self.objects.get(endpoint, [])
        filters = dict(filters)
        device_ids = filters.pop("device_id", None) if endpoint == IP_ADDRESSES else None
        matched = [record for record in records if self._matches(record, filters)]
        if device_ids is None:
            return matched
        wanted_ids = device_ids if isinstance(device_ids, list) else [device_ids]
        wanted = {int(value) for value in wanted_ids}
        return [record for record in matched if self._ip_device_id(record) in wanted]

    def count(self, endpoint: str, **filters: Any) -> int:
        return len(self.get_all(endpoint, **filters))

    def choices_for(self, endpoint: str, field_name: str) -> frozenset[str]:
        if endpoint not in self._choices_fetched:
            # One OPTIONS request per endpoint for the life of the client, as the real one does.
            self._choices_fetched.add(endpoint)
            self.request_count += 1
        return CHOICES.get(endpoint, {}).get(field_name, frozenset())

    def create(self, endpoint: str, payload: dict | list[dict]) -> list[dict[str, Any]]:
        self.request_count += 1
        payloads = payload if isinstance(payload, list) else [payload]
        self.posts.append(Post(endpoint=endpoint, payloads=[dict(item) for item in payloads]))

        if endpoint in self.unavailable_on:
            raise self.unavailable_on[endpoint]

        decide = self.rejections.get(endpoint)
        if decide is not None:
            verdict = decide(payloads)
            if verdict is not None:
                status, body = verdict
                # Atomic: a rejected batch stores nothing (decision D7).
                raise NetBoxWriteRejected(status, body, endpoint)

        created: list[dict[str, Any]] = []
        for item in payloads:
            record = {"id": self._take_id(), **self._expand(item)}
            self.objects.setdefault(endpoint, []).append(record)
            if endpoint == CABLES:
                self._occupy_terminations(record)
            if endpoint == DEVICES:
                self._inherit_interfaces(record)
            if endpoint == INTERFACES:
                self._normalise_interface(record)
            if endpoint == IP_ADDRESSES:
                self._bind_ip(record)
            created.append(record)
        return created

    def patch(self, endpoint: str, payload: dict | list[dict]) -> list[dict[str, Any]]:
        self.request_count += 1
        payloads = payload if isinstance(payload, list) else [payload]
        self.patches.append(Post(endpoint=endpoint, payloads=[dict(item) for item in payloads]))
        if endpoint in self.unavailable_on:
            raise self.unavailable_on[endpoint]
        decide = self.rejections.get(f"PATCH:{endpoint}")
        if decide is not None:
            verdict = decide(payloads)
            if verdict is not None:
                status, body = verdict
                raise NetBoxWriteRejected(status, body, endpoint)
        return [self._apply_primary_patch(item) for item in payloads]

    def _inherit_interfaces(self, device: dict[str, Any]) -> None:
        """What NetBox does on device create: copy the type's interface templates."""
        device_type = device.get("device_type")
        type_id = device_type.get("id") if isinstance(device_type, dict) else device_type
        if type_id is None:
            return
        for template in self.objects.get(INTERFACE_TEMPLATES, []):
            held = template.get("device_type")
            held_id = held.get("id") if isinstance(held, dict) else held
            if held_id != type_id:
                continue
            iface_type = template.get("type")
            if isinstance(iface_type, str):
                iface_type = {"value": iface_type, "label": iface_type}
            self.objects.setdefault(INTERFACES, []).append(
                {
                    "id": self._take_id(),
                    "device": {"id": device["id"], "name": device.get("name")},
                    "name": template.get("name"),
                    "type": iface_type,
                    "mgmt_only": bool(template.get("mgmt_only")),
                }
            )

    def _normalise_interface(self, record: dict[str, Any]) -> None:
        """An interface POSTed directly has to look like one NetBox served (D12).

        `_expand` has already turned the submitted `device` id into a brief. What is left is the
        shape of `type` — submitted as a bare value, returned by NetBox as `{value, label}` — and
        the `cable` key every termination carries. Without this, an interface created by the
        cabling sheet's first batch would not be readable by the cable batch that follows it in the
        same apply, and the two-endpoint ordering of Requirement 9.32 could not be tested at all.
        """
        iface_type = record.get("type")
        if isinstance(iface_type, str):
            record["type"] = {"value": iface_type, "label": iface_type}
        record.setdefault("cable", None)

    def _bind_ip(self, record: dict[str, Any]) -> None:
        object_id = record.get("assigned_object_id")
        object_type = record.get("assigned_object_type") or "dcim.interface"
        record["assigned_object_type"] = object_type
        if object_id is None:
            return
        for iface in self.objects.get(INTERFACES, []):
            if iface.get("id") == object_id:
                record["assigned_object"] = {
                    "id": object_id,
                    "name": iface.get("name"),
                    "device": iface.get("device"),
                }
                return

    def _ip_device_id(self, record: dict[str, Any]) -> int | None:
        assigned = record.get("assigned_object") or {}
        device = assigned.get("device") if isinstance(assigned, dict) else None
        if isinstance(device, dict) and device.get("id") is not None:
            return int(device["id"])
        object_id = record.get("assigned_object_id")
        if record.get("assigned_object_type") != "dcim.interface" or object_id is None:
            return None
        for iface in self.objects.get(INTERFACES, []):
            if iface.get("id") != object_id:
                continue
            held = iface.get("device")
            held_id = held.get("id") if isinstance(held, dict) else held
            return int(held_id) if held_id is not None else None
        return None

    def _apply_primary_patch(self, item: dict[str, Any]) -> dict[str, Any]:
        object_id = int(item["id"])
        for record in self.objects.get(DEVICES, []):
            if record.get("id") != object_id:
                continue
            for ip_field in ("primary_ip4", "primary_ip6"):
                if ip_field not in item:
                    continue
                ip_id = item[ip_field]
                ip_record = next(
                    (ip for ip in self.objects.get(IP_ADDRESSES, []) if ip.get("id") == ip_id),
                    None,
                )
                record[ip_field] = (
                    {"id": ip_id, "address": ip_record.get("address")}
                    if ip_record is not None
                    else {"id": ip_id}
                )
            return record
        return {"id": object_id, **{k: v for k, v in item.items() if k != "id"}}

    def _occupy_terminations(self, cable: dict[str, Any]) -> None:
        """What NetBox does for you: a cabled port carries the cable's id from then on."""
        for side in ("a_terminations", "b_terminations"):
            for termination in cable.get(side) or []:
                endpoint = TERMINATION_ENDPOINTS.get(termination.get("object_type", ""))
                if endpoint is None:
                    continue
                for record in self.objects.get(endpoint, []):
                    if record.get("id") == termination.get("object_id"):
                        record["cable"] = {"id": cable["id"]}

    def object_url(self, endpoint: str, object_id: int) -> str:
        app, _, name = endpoint.partition(".")
        return f"https://{self.host}/{app}/{name}/{object_id}/"

    # --- filtering, faithful to NetBox's quirks ---------------------------------------------

    def _matches(self, record: dict[str, Any], filters: dict[str, Any]) -> bool:
        for name, wanted in filters.items():
            values = wanted if isinstance(wanted, list) else [wanted]
            if name.endswith("__ie"):
                field_name = name.removesuffix("__ie")
                held = str(record.get(field_name, "")).casefold()
                if held not in {str(value).casefold() for value in values}:
                    return False
            elif name.endswith("_id"):
                field_name = name.removesuffix("_id")
                related = record.get(field_name)
                held = related.get("id") if isinstance(related, dict) else related
                if held not in {int(value) for value in values}:
                    return False
            elif name == "mgmt_only":
                wanted_bool = {str(value).casefold() in {"1", "true", "yes"} for value in values}
                if bool(record.get("mgmt_only")) not in wanted_bool:
                    return False
            elif name == "type":
                held = record.get("type")
                held_value = held.get("value") if isinstance(held, dict) else held
                if str(held_value).casefold() not in {str(value).casefold() for value in values}:
                    return False
            elif name == "address":
                held = str(record.get("address") or "")
                if held.casefold() not in {str(value).casefold() for value in values}:
                    return False
            # Anything else is ignored, exactly as NetBox ignores an unrecognised parameter: the
            # response comes back unfiltered rather than as an error.
        return True

    def _expand(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Turn submitted ids into the nested briefs NetBox returns, so reads look realistic."""
        expanded: dict[str, Any] = {}
        for name, value in payload.items():
            if name in {
                "site",
                "region",
                "location",
                "rack",
                "tenant",
                "device",
                "device_type",
                "role",
            }:
                expanded[name] = self._brief(name, value)
            else:
                expanded[name] = value
        return expanded

    def _brief(self, name: str, value: Any) -> Any:
        if not isinstance(value, int):
            return value
        endpoint = {
            "site": "dcim.sites",
            "region": "dcim.regions",
            "location": "dcim.locations",
            "rack": "dcim.racks",
            "tenant": "tenancy.tenants",
            "device": "dcim.devices",
            "device_type": "dcim.device-types",
            "role": "dcim.device-roles",
        }[name]
        for record in self.objects.get(endpoint, []):
            if record.get("id") == value:
                return {
                    "id": value,
                    "name": record.get("name"),
                    "slug": record.get("slug"),
                    "model": record.get("model"),
                }
        return {"id": value}
