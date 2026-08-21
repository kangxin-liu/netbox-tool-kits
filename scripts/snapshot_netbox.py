#!/usr/bin/env python3
# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Export and restore a rebuildable snapshot of core NetBox inventory.

This is an API-level seed snapshot, not a database backup. It intentionally excludes users,
tokens, permissions, change logs, secrets, plugin data, and other database-internal state.

Examples:
    python3 scripts/snapshot_netbox.py export
    python3 scripts/snapshot_netbox.py restore netbox-snapshots/netbox-inventory.json
    python3 scripts/snapshot_netbox.py restore netbox-snapshots/netbox-inventory.json --apply

Restore is a dry run unless --apply is supplied. A non-empty target is refused unless --merge is
also supplied, and a remote target additionally requires --allow-remote.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SNAPSHOT = REPO_ROOT / "netbox-snapshots" / f"netbox-inventory-{datetime.now().strftime("%Y%m%d%H%M%S")}.json"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
SUPPORTED_VERSION = (4, 6)
FORMAT_VERSION = 1

# Parent objects precede their dependants. A missing endpoint is treated as a compatibility
# difference and recorded in the snapshot; authorization and other errors remain fatal.
ENDPOINTS = (
    "extras/tags",
    "extras/custom-field-choice-sets",
    "extras/custom-fields",
    "tenancy/tenant-groups",
    "tenancy/tenants",
    "dcim/regions",
    "dcim/site-groups",
    "dcim/sites",
    "dcim/location-types",
    "dcim/locations",
    "dcim/rack-roles",
    "dcim/rack-types",
    "dcim/racks",
    "dcim/manufacturers",
    "dcim/device-roles",
    "dcim/platforms",
    "dcim/device-types",
    "dcim/module-types",
    "dcim/console-port-templates",
    "dcim/console-server-port-templates",
    "dcim/power-port-templates",
    "dcim/power-outlet-templates",
    "dcim/interface-templates",
    "dcim/rear-port-templates",
    "dcim/front-port-templates",
    "dcim/module-bay-templates",
    "dcim/device-bay-templates",
    "dcim/inventory-item-templates",
    "dcim/virtual-chassis",
    "dcim/devices",
    "dcim/modules",
    "dcim/console-ports",
    "dcim/console-server-ports",
    "dcim/power-ports",
    "dcim/power-outlets",
    "dcim/interfaces",
    "dcim/rear-ports",
    "dcim/front-ports",
    "dcim/module-bays",
    "dcim/device-bays",
    "dcim/inventory-items",
    "dcim/power-panels",
    "dcim/power-feeds",
    "ipam/roles",
    "ipam/rirs",
    "ipam/route-targets",
    "ipam/vrfs",
    "ipam/aggregates",
    "ipam/asn-ranges",
    "ipam/asns",
    "ipam/vlan-groups",
    "ipam/vlans",
    "ipam/prefixes",
    "ipam/ip-ranges",
    "ipam/fhrp-groups",
    "ipam/ip-addresses",
    "ipam/service-templates",
    "ipam/services",
    "ipam/fhrp-group-assignments",
    "dcim/cables",
)

# These fields create legitimate cycles and are attached only after all objects exist.
DELAYED_FIELDS = {
    "dcim/devices": {"primary_ip4", "primary_ip6", "oob_ip", "virtual_chassis", "vc_position"},
    "dcim/virtual-chassis": {"master"},
}

OBJECT_TYPE_ENDPOINTS = {
    "dcim.region": "dcim/regions",
    "dcim.sitegroup": "dcim/site-groups",
    "dcim.site": "dcim/sites",
    "dcim.location": "dcim/locations",
    "dcim.rack": "dcim/racks",
    "dcim.device": "dcim/devices",
    "dcim.virtualchassis": "dcim/virtual-chassis",
    "dcim.consoleport": "dcim/console-ports",
    "dcim.consoleserverport": "dcim/console-server-ports",
    "dcim.powerport": "dcim/power-ports",
    "dcim.poweroutlet": "dcim/power-outlets",
    "dcim.interface": "dcim/interfaces",
    "dcim.frontport": "dcim/front-ports",
    "dcim.rearport": "dcim/rear-ports",
    "dcim.powerfeed": "dcim/power-feeds",
    "virtualization.vminterface": "virtualization/interfaces",
    "circuits.circuittermination": "circuits/circuit-terminations",
}

URL_ID_RE = re.compile(r"/api/(?P<endpoint>.+)/(?P<id>\d+)/?$")


class SnapshotError(RuntimeError):
    """A safe, operator-facing snapshot or restore error."""


class MissingReference(SnapshotError):
    """A relationship cannot be mapped until another record has been restored."""


@dataclass(frozen=True)
class Settings:
    url: str
    token: str
    verify_ssl: bool
    timeout: int


def parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SnapshotError(f"{name} must be true or false")


def load_settings() -> Settings:
    values = dict(os.environ)
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for raw_line in env_file.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip())

    url = values.get("NETBOX_URL", "").strip()
    token = values.get("NETBOX_TOKEN", "").strip()
    if not url or not token:
        raise SnapshotError("NETBOX_URL and NETBOX_TOKEN must be set in the environment or .env")

    verify_ssl = parse_bool(values.get("NETBOX_VERIFY_SSL", "true"), "NETBOX_VERIFY_SSL")
    try:
        timeout = int(values.get("NETBOX_TIMEOUT", "30"))
    except ValueError as exc:
        raise SnapshotError("NETBOX_TIMEOUT must be an integer") from exc
    if timeout < 1:
        raise SnapshotError("NETBOX_TIMEOUT must be positive")

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise SnapshotError("NETBOX_URL must be an absolute http:// or https:// URL")
    if parsed.scheme != "https" and host not in LOOPBACK_HOSTS:
        raise SnapshotError("Plain HTTP is permitted only for a loopback NetBox instance")
    if not verify_ssl:
        print("WARNING: TLS certificate verification is disabled", file=sys.stderr)
    return Settings(url=url.rstrip("/"), token=token, verify_ssl=verify_ssl, timeout=timeout)


class NetBox:
    def __init__(self, settings: Settings):
        self.base = settings.url
        self.token = settings.token
        self.timeout = settings.timeout
        self.context = (
            ssl.create_default_context()
            if settings.verify_ssl
            else ssl._create_unverified_context()
        )
        self._writable_fields: dict[str, set[str]] = {}

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_not_found: bool = False,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base}{path}"
        if url.startswith("http"):
            parsed_url = urlparse(url)
            parsed_base = urlparse(self.base)
            if (parsed_url.scheme, parsed_url.netloc) != (parsed_base.scheme, parsed_base.netloc):
                raise SnapshotError("NetBox pagination attempted to leave the configured origin")
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Token {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.context
            ) as response:
                return json.load(response) if response.status != 204 else None
        except urllib.error.HTTPError as exc:
            if allow_not_found and exc.code == 404:
                return None
            body = exc.read().decode(errors="replace")[:500]
            raise SnapshotError(f"NetBox {exc.code} on {method} {path}: {body}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise SnapshotError(f"Unable to reach NetBox for {method} {path}: {reason}") from None

    def status(self) -> dict[str, Any]:
        return self.request("GET", "/api/status/")

    def all(self, endpoint: str, *, allow_not_found: bool = False) -> list[dict[str, Any]] | None:
        page = self.request("GET", f"/api/{endpoint}/?limit=500", allow_not_found=allow_not_found)
        if page is None:
            return None
        records: list[dict[str, Any]] = []
        while True:
            records.extend(page.get("results", []))
            next_url = page.get("next")
            if not next_url:
                return records
            page = self.request("GET", next_url)

    def writable_fields(self, endpoint: str) -> set[str]:
        if endpoint not in self._writable_fields:
            metadata = self.request("OPTIONS", f"/api/{endpoint}/")
            fields = metadata.get("actions", {}).get("POST")
            if not isinstance(fields, dict):
                raise SnapshotError(
                    f"NetBox did not advertise POST fields for /api/{endpoint}/; "
                    "check token permissions"
                )
            self._writable_fields[endpoint] = {
                name for name, spec in fields.items() if not spec.get("read_only", False)
            }
        return self._writable_fields[endpoint]

    def create(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/{endpoint}/", payload)

    def patch(self, endpoint: str, object_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"/api/{endpoint}/{object_id}/", payload)


def check_version(version: str) -> None:
    try:
        major_minor = tuple(int(part) for part in version.split(".")[:2])
    except ValueError as exc:
        raise SnapshotError(f"Unrecognised NetBox version: {version}") from exc
    if major_minor != SUPPORTED_VERSION:
        raise SnapshotError(
            f"NetBox {version} is outside the supported 4.6.x line; "
            "serializer fields may not be restore-compatible"
        )


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def checksum(snapshot_without_checksum: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(snapshot_without_checksum)).hexdigest()


def export_snapshot(client: NetBox, output: Path) -> None:
    status = client.status()
    version = str(status.get("netbox-version", "unknown"))
    check_version(version)

    endpoint_data: list[dict[str, Any]] = []
    total = 0
    for endpoint in ENDPOINTS:
        records = client.all(endpoint, allow_not_found=True)
        if records is None:
            print(f"  unavailable  {endpoint}")
            continue
        endpoint_data.append({"path": endpoint, "records": records})
        total += len(records)
        print(f"  {len(records):>6}  {endpoint}")

    snapshot: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "classification": "CONFIDENTIAL",
        "source": {
            "netbox_version": version,
            "exported_at": datetime.now(UTC).isoformat(),
            "scope": "core tenancy, DCIM, cabling, and basic IPAM inventory",
        },
        "endpoints": endpoint_data,
    }
    snapshot["sha256"] = checksum(snapshot)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(snapshot, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    temporary.replace(output)
    print(f"\nExported {total} records to {output}")
    print("Keep this CONFIDENTIAL file outside version control and verify it before teardown.")


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"Cannot read snapshot {path}: {exc}") from exc
    if snapshot.get("format_version") != FORMAT_VERSION:
        raise SnapshotError("Unsupported snapshot format version")
    expected = snapshot.pop("sha256", None)
    actual = checksum(snapshot)
    snapshot["sha256"] = expected
    if not isinstance(expected, str) or expected != actual:
        raise SnapshotError("Snapshot checksum does not match; the file is incomplete or modified")
    if snapshot.get("classification") != "CONFIDENTIAL":
        raise SnapshotError("Snapshot is missing its CONFIDENTIAL classification marker")
    if not isinstance(snapshot.get("endpoints"), list):
        raise SnapshotError("Snapshot endpoints must be a list")
    return snapshot


def reference_key(value: dict[str, Any]) -> tuple[str, int] | None:
    url = value.get("url")
    object_id = value.get("id")
    if not isinstance(url, str) or not isinstance(object_id, int):
        return None
    match = URL_ID_RE.search(urlparse(url).path)
    if not match:
        return None
    return match.group("endpoint"), int(match.group("id"))


class Restorer:
    def __init__(
        self,
        client: NetBox,
        snapshot: dict[str, Any],
        *,
        merge: bool,
    ):
        self.client = client
        self.merge = merge
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.old_keys: set[tuple[str, int]] = set()
        self.id_map: dict[tuple[str, int], int] = {}
        self.created_keys: set[tuple[str, int]] = set()
        self.target_records: dict[str, list[dict[str, Any]]] = {}
        self.target_identities: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
        self.created = 0
        self.matched = 0
        self.delayed: list[tuple[str, dict[str, Any], tuple[str, int]]] = []

        seen_endpoints: set[str] = set()
        for entry in snapshot["endpoints"]:
            endpoint = entry.get("path")
            records = entry.get("records")
            if (
                endpoint not in ENDPOINTS
                or endpoint in seen_endpoints
                or not isinstance(records, list)
            ):
                raise SnapshotError(f"Invalid or duplicate endpoint in snapshot: {endpoint!r}")
            seen_endpoints.add(endpoint)
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("id"), int):
                    raise SnapshotError(f"Invalid record in snapshot endpoint {endpoint}")
                key = (endpoint, record["id"])
                if key in self.old_keys:
                    raise SnapshotError(f"Duplicate source object {endpoint}/{record['id']}")
                self.old_keys.add(key)
                self.records.append((endpoint, record))

    def map_reference(self, endpoint: str, old_id: int) -> int:
        key = (endpoint, old_id)
        if key in self.id_map:
            return self.id_map[key]
        if key in self.old_keys:
            raise MissingReference(f"{endpoint}/{old_id} is not restored yet")
        raise SnapshotError(
            f"Snapshot is incomplete: relationship points to uncaptured {endpoint}/{old_id}"
        )

    def normalize(
        self,
        value: Any,
        *,
        endpoint: str,
        field: str,
    ) -> Any:
        if isinstance(value, list):
            return [self.normalize(item, endpoint=endpoint, field=field) for item in value]
        if not isinstance(value, dict):
            return value

        key = reference_key(value)
        if key is not None:
            return self.map_reference(*key)
        if "value" in value and set(value).issuperset({"value", "label"}):
            return value["value"]

        if isinstance(value.get("object_type"), str) and isinstance(value.get("object_id"), int):
            object_endpoint = OBJECT_TYPE_ENDPOINTS.get(value["object_type"])
            if object_endpoint is None:
                raise SnapshotError(
                    f"Unsupported generic relationship type: {value['object_type']}"
                )
            return {
                "object_type": value["object_type"],
                "object_id": self.map_reference(object_endpoint, value["object_id"]),
            }

        normalized: dict[str, Any] = {}
        for key_name, nested in value.items():
            if key_name in {"display", "url", "object"}:
                continue
            if key_name == "rear_port" and isinstance(nested, int):
                rear_endpoint = (
                    "dcim/rear-port-templates"
                    if endpoint == "dcim/front-port-templates"
                    else "dcim/rear-ports"
                )
                normalized[key_name] = self.map_reference(rear_endpoint, nested)
            else:
                normalized[key_name] = self.normalize(nested, endpoint=endpoint, field=key_name)
        return normalized

    def payload(
        self,
        endpoint: str,
        record: dict[str, Any],
        *,
        delayed_only: bool = False,
    ) -> dict[str, Any]:
        writable = self.client.writable_fields(endpoint)
        delayed_fields = DELAYED_FIELDS.get(endpoint, set())
        selected_fields = delayed_fields if delayed_only else writable - delayed_fields
        payload: dict[str, Any] = {}
        for field in selected_fields:
            if field not in record:
                continue
            if field.endswith("_id"):
                type_field = field.removesuffix("_id") + "_type"
                old_id = record[field]
                object_type = record.get(type_field)
                if old_id is not None and isinstance(object_type, str):
                    object_endpoint = OBJECT_TYPE_ENDPOINTS.get(object_type)
                    if object_endpoint is None:
                        raise SnapshotError(f"Unsupported generic relationship type: {object_type}")
                    payload[field] = self.map_reference(object_endpoint, old_id)
                    continue
            payload[field] = self.normalize(record[field], endpoint=endpoint, field=field)
        return payload

    @staticmethod
    def local_value(value: Any) -> Any:
        if isinstance(value, list):
            return tuple(Restorer.local_value(item) for item in value)
        if isinstance(value, dict):
            key = reference_key(value)
            if key is not None:
                return value["id"]
            if "value" in value and "label" in value:
                return value["value"]
            return tuple(
                (name, Restorer.local_value(nested))
                for name, nested in sorted(value.items())
                if name not in {"display", "url", "object"}
            )
        return value

    @staticmethod
    def identity_fields(endpoint: str, value: dict[str, Any]) -> tuple[str, ...]:
        if value.get("slug"):
            return ("slug",)
        explicit = {
            "dcim/cables": ("a_terminations", "b_terminations"),
            "ipam/aggregates": ("prefix",),
            "ipam/asn-ranges": ("rir", "start", "end"),
            "ipam/asns": ("asn",),
            "ipam/route-targets": ("name",),
            "ipam/vrfs": ("rd",),
            "ipam/vlan-groups": ("scope_type", "scope_id", "name"),
            "ipam/vlans": ("group", "vid"),
            "ipam/prefixes": ("vrf", "prefix"),
            "ipam/ip-ranges": ("vrf", "start_address", "end_address"),
            "ipam/ip-addresses": ("vrf", "address"),
            "ipam/fhrp-groups": ("protocol", "group_id"),
            "ipam/fhrp-group-assignments": ("group", "interface_type", "interface_id"),
            "ipam/services": ("device", "virtual_machine", "name", "protocol", "ports"),
        }.get(endpoint)
        if explicit is not None:
            return explicit
        if endpoint.endswith("-templates"):
            owner = "device_type" if value.get("device_type") is not None else "module_type"
            return owner, "name"
        if endpoint in {
            "dcim/console-ports",
            "dcim/console-server-ports",
            "dcim/power-ports",
            "dcim/power-outlets",
            "dcim/interfaces",
            "dcim/rear-ports",
            "dcim/front-ports",
            "dcim/module-bays",
            "dcim/device-bays",
        }:
            return "device", "name"
        if endpoint == "dcim/inventory-items":
            return "device", "parent", "name"
        if endpoint == "dcim/device-types":
            return "manufacturer", "model"
        if endpoint == "dcim/module-types":
            return "manufacturer", "model"
        if endpoint == "dcim/devices":
            return "site", "name"
        if endpoint == "dcim/racks":
            return "site", "location", "name"
        if endpoint == "dcim/locations":
            return "site", "parent", "name"
        if value.get("name") is not None:
            return ("name",)
        return ()

    def identity(
        self,
        endpoint: str,
        value: dict[str, Any],
        *,
        source_payload: bool,
    ) -> tuple[Any, ...] | None:
        fields = self.identity_fields(endpoint, value)
        if not fields:
            return None
        if source_payload:
            return tuple(self.local_value(value.get(field)) for field in fields)
        return tuple(self.local_value(value.get(field)) for field in fields)

    def load_target(self, endpoint: str) -> None:
        if endpoint in self.target_records:
            return
        records = self.client.all(endpoint)
        assert records is not None
        self.target_records[endpoint] = records
        identities: dict[tuple[Any, ...], dict[str, Any]] = {}
        for record in records:
            identity = self.identity(endpoint, record, source_payload=False)
            if identity is not None:
                identities[identity] = record
        self.target_identities[endpoint] = identities

    def find_existing(
        self,
        endpoint: str,
        record: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        self.load_target(endpoint)
        identity = self.identity(endpoint, payload, source_payload=True)
        if identity is None:
            return None
        return self.target_identities[endpoint].get(identity)

    def preflight(self) -> None:
        nonempty: list[str] = []
        endpoints_with_records = {
            endpoint for endpoint, record in self.records if isinstance(record.get("id"), int)
        }
        for endpoint in ENDPOINTS:
            if endpoint not in endpoints_with_records:
                continue
            self.load_target(endpoint)
            if self.target_records[endpoint]:
                nonempty.append(f"{endpoint} ({len(self.target_records[endpoint])})")
        if nonempty and not self.merge:
            details = ", ".join(nonempty[:8])
            suffix = " ..." if len(nonempty) > 8 else ""
            raise SnapshotError(
                f"Target is not empty: {details}{suffix}. "
                "Use a fresh target or pass --merge to map matching objects "
                "and create missing ones."
            )
        # Device creation can automatically materialise interfaces and other components from its
        # type's templates. Do not retain the preflight's empty component caches into the apply
        # phase, or those generated records would be mistaken for missing records and duplicated.
        self.target_records.clear()
        self.target_identities.clear()

    def validate_relationships(self) -> None:
        """Prove every writable captured relationship points to another captured object."""
        self.id_map = {key: key[1] for key in self.old_keys}
        try:
            for endpoint, record in self.records:
                self.payload(endpoint, record)
                if DELAYED_FIELDS.get(endpoint):
                    self.payload(endpoint, record, delayed_only=True)
        finally:
            self.id_map.clear()

    def restore(self) -> None:
        pending = list(self.records)
        while pending:
            next_pending: list[tuple[str, dict[str, Any]]] = []
            progress = 0
            last_missing = ""
            for endpoint, record in pending:
                source_key = (endpoint, record["id"])
                try:
                    payload = self.payload(endpoint, record)
                except MissingReference as exc:
                    next_pending.append((endpoint, record))
                    last_missing = str(exc)
                    continue

                existing = self.find_existing(endpoint, record, payload)
                if existing is not None:
                    self.id_map[source_key] = existing["id"]
                    self.matched += 1
                else:
                    created = self.client.create(endpoint, payload)
                    self.id_map[source_key] = created["id"]
                    self.created_keys.add(source_key)
                    self.created += 1
                    # Keep the in-memory identity index current for later siblings.
                    self.target_records[endpoint].append(created)
                    identity = self.identity(endpoint, created, source_payload=False)
                    if identity is not None:
                        self.target_identities[endpoint][identity] = created
                if DELAYED_FIELDS.get(endpoint):
                    self.delayed.append((endpoint, record, source_key))
                progress += 1

            if progress == 0:
                raise SnapshotError(
                    f"Cannot resolve {len(next_pending)} relationships; last dependency: "
                    f"{last_missing or 'unknown'}"
                )
            pending = next_pending

        patched = 0
        for endpoint, record, source_key in self.delayed:
            if source_key not in self.created_keys:
                continue
            payload = self.payload(endpoint, record, delayed_only=True)
            payload = {name: value for name, value in payload.items() if value is not None}
            if payload:
                self.client.patch(endpoint, self.id_map[source_key], payload)
                patched += 1
        print(
            f"Restored {self.created} records; matched {self.matched} existing records; "
            f"completed {patched} delayed relationship updates."
        )


def snapshot_counts(snapshot: dict[str, Any]) -> tuple[int, list[str]]:
    lines: list[str] = []
    total = 0
    for entry in snapshot["endpoints"]:
        count = len(entry["records"])
        total += count
        if count:
            lines.append(f"  {count:>6}  {entry['path']}")
    return total, lines


def restore_snapshot(
    client: NetBox,
    path: Path,
    *,
    apply: bool,
    merge: bool,
    allow_remote: bool,
) -> None:
    snapshot = load_snapshot(path)
    source_version = str(snapshot.get("source", {}).get("netbox_version", "unknown"))
    check_version(source_version)
    target_version = str(client.status().get("netbox-version", "unknown"))
    check_version(target_version)
    if target_version.split(".")[:2] != source_version.split(".")[:2]:
        raise SnapshotError(
            f"Snapshot NetBox {source_version} and target {target_version} are not API-compatible"
        )

    host = (urlparse(client.base).hostname or "").lower()
    if apply and host not in LOOPBACK_HOSTS and not allow_remote:
        raise SnapshotError(
            f"Refusing to write to remote host {host}; pass --allow-remote only after "
            "verifying the target and snapshot"
        )

    total, lines = snapshot_counts(snapshot)
    print(f"Snapshot: {path}")
    print(f"Source NetBox: {source_version}; target NetBox: {target_version}")
    print(f"Records: {total}")
    for line in lines:
        print(line)

    restorer = Restorer(client, snapshot, merge=merge)
    restorer.validate_relationships()
    restorer.preflight()
    if not apply:
        print(
            "\nDry run only: snapshot integrity, relationships, version, and target checks passed."
        )
        print("Re-run with --apply to restore. No NetBox records were written.")
        return

    print("\nAPPLY mode: creating inventory records...")
    restorer.restore()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="capture current inventory")
    export_parser.add_argument(
        "output", nargs="?", type=Path, default=DEFAULT_SNAPSHOT, help="snapshot JSON path"
    )

    restore_parser = subparsers.add_parser("restore", help="seed inventory from a snapshot")
    restore_parser.add_argument("snapshot", type=Path, help="snapshot JSON path")
    restore_parser.add_argument("--apply", action="store_true", help="actually write records")
    restore_parser.add_argument(
        "--merge",
        action="store_true",
        help="allow a non-empty target; match stable identities and create missing records",
    )
    restore_parser.add_argument(
        "--allow-remote", action="store_true", help="permit writes to a non-loopback target"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        client = NetBox(load_settings())
        if args.command == "export":
            export_snapshot(client, args.output.resolve())
        else:
            restore_snapshot(
                client,
                args.snapshot.resolve(),
                apply=args.apply,
                merge=args.merge,
                allow_remote=args.allow_remote,
            )
    except SnapshotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
