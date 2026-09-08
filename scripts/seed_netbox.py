#!/usr/bin/env python3
# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Add the missing port templates to the device types that already exist in NetBox.

WHY THIS EXISTS
---------------
A NetBox device type is a template. Creating a device from a type whose type defines no ports
produces a device with no ports, and the cabling importer then has nothing to terminate against.
The device types in the target instance were created without any interface, front-port, or
rear-port templates, so this script fills that gap.

ORDERING MATTERS: NetBox does not retro-fit ports onto devices that already exist. Run this
BEFORE creating any devices, or you will be deleting and recreating them.

SAFETY
------
* Dry run by default. Nothing is written without --apply.
* Refuses any target that is not a loopback host unless --allow-remote is passed, mirroring the
  loopback-only exception in the spec (requirement 2.3a).
* Idempotent: a template that already exists by name is skipped, so re-running is safe.
* Creates nothing except component templates on device types that already exist. It never creates
  device types, devices, sites, or racks — those come from the importer sheets.

USAGE
-----
    python3 scripts/seed_netbox.py                    # dry run, shows the plan
    python3 scripts/seed_netbox.py --apply            # write it
    python3 scripts/seed_netbox.py --naming generic    # Ethernet1 instead of swp1

INTERFACE NAMING
----------------
--naming selects the switch interface naming scheme. This MUST match whatever you will put in the
cabling sheet's a_port / b_port columns, or every cabling row will fail to resolve a port.
    cumulus  swp1, swp2, ...        (Cumulus Linux — the default)
    generic  Ethernet1, Ethernet2, ...
If you run NVOS or Onyx, add a scheme to NAMING_SCHEMES below rather than renaming ports by hand.

BREAKOUT MODELLING
------------------
Switch templates define the PHYSICAL cages only — one interface per QSFP-DD/OSFP cage, not the
lanes it can split into. NetBox 4.5+ expresses breakout at the cable level via cable profiles,
which keeps the device model honest about the hardware. If you would rather model each lane as its
own child interface, that is a different decision and this script is the wrong place for it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

# Switch interface naming. The key is passed via --naming.
NAMING_SCHEMES: dict[str, str] = {
    "cumulus": "swp{n}",
    "generic": "Ethernet{n}",
}

# Management and loopback templates per naming scheme. The device importer assigns
# loopback_ip* / management_ip* to these (mgmt_only / type=loopback), not to the cage ports.
MGMT_LOOPBACK: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "cumulus": (("eth0", "1000base-t", True), ("lo", "loopback", False)),
    "generic": (("Management1", "1000base-t", True), ("Loopback0", "loopback", False)),
}

# Physical cages per switch model, as ranges of (count, netbox_interface_type).
# Sources: NVIDIA hardware manuals — SN2201 48x1GbE RJ45 + 4x100GbE QSFP28;
# SN3700 32x200GbE QSFP56; SN4700 32x400GbE QSFP-DD; SN5610 64x800GbE OSFP + 2x25GbE SFP28.
SWITCH_INTERFACES: dict[str, tuple[tuple[int, str], ...]] = {
    "Spectrum-SN2201": ((48, "1000base-t"), (4, "100gbase-x-qsfp28")),
    "Spectrum-SN3700": ((32, "200gbase-x-qsfp56"),),
    "Spectrum-SN4700": ((32, "400gbase-x-qsfpdd"),),
    "Spectrum-SN5610": ((64, "800gbase-x-osfp"), (2, "25gbase-x-sfp28")),
}

# Fibre connector types, validated against this instance's choices: only bare "mpo" exists (there
# is no mpo-16 value), and LC has lc / lc-pc / lc-upc / lc-apc. Bare "lc" is the least-wrong
# default for "LC-LC-SM"; change to lc-upc or lc-apc if your panels are specified that way.
TYPE_MPO = "mpo"
TYPE_LC = "lc"


class Panel:
    """A patch panel device type's front/rear port layout."""

    def __init__(self, model: str, description: str):
        self.model = model
        self.description = description
        self.rear: list[dict[str, Any]] = []
        self.front: list[dict[str, Any]] = []

    def add_rear(self, name: str, port_type: str, positions: int = 1) -> None:
        self.rear.append({"name": name, "type": port_type, "positions": positions})

    def add_front(self, name: str, port_type: str, rear_name: str, rear_position: int = 1) -> None:
        self.front.append(
            {
                "name": name,
                "type": port_type,
                "positions": 1,
                "rear_name": rear_name,
                "rear_position": rear_position,
            }
        )


def build_panels() -> list[Panel]:
    panels: list[Panel] = []

    # 1:1 LC panel. Front Fn maps straight through to Rear Rn.
    lc = Panel(
        "Patch Panel - 24 pair LC-LC-SM - 1U",
        "24 LC duplex pairs, front to rear 1:1",
    )
    for n in range(1, 25):
        lc.add_rear(f"R{n}", TYPE_LC)
        lc.add_front(f"F{n}", TYPE_LC, f"R{n}")
    panels.append(lc)

    # 1:1 MPO trunk panel. Front and rear share names; the importer's *_port_side column is what
    # disambiguates them, which is exactly why that column exists.
    mpo = Panel(
        "Patch Panel - MPO16 24Port - 1U",
        "24 MPO ports, front to rear 1:1",
    )
    for n in range(1, 25):
        mpo.add_rear(f"MPO{n}", TYPE_MPO)
        mpo.add_front(f"MPO{n}", TYPE_MPO, f"MPO{n}")
    panels.append(mpo)

    # The breakout panel: 8 rear MPO trunks, each fanning out to 4 LC duplex front ports.
    # MPO1 -> 1A, 1B, 1C, 1D on rear positions 1..4; MPO2 -> 2A..2D; and so on to MPO8 -> 8A..8D.
    # The rear port carries positions=4 because four front ports terminate against it.
    breakout = Panel(
        "Patch Panel - 8 Group 4x Breakout 1U",
        "8 MPO trunks, each breaking out to 4 LC duplex pairs (32 front ports)",
    )
    for group in range(1, 9):
        rear_name = f"MPO{group}"
        breakout.add_rear(rear_name, TYPE_MPO, positions=4)
        for index, suffix in enumerate("ABCD", start=1):
            breakout.add_front(f"{group}{suffix}", TYPE_LC, rear_name, rear_position=index)
    panels.append(breakout)

    return panels


class NetBox:
    def __init__(self, base: str, token: str, apply: bool):
        self.base = base.rstrip("/")
        self.token = token
        self.apply = apply
        self.created = 0
        self.skipped = 0

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Token {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response) if response.status != 204 else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:500]
            # Deliberately does not echo request headers: they carry the API token.
            raise SystemExit(f"NetBox {exc.code} on {method} {path}\n{body}") from None

    def get(self, path: str, **params: Any) -> Any:
        query = urllib.parse.urlencode(params)
        return self._request("GET", f"{path}?{query}" if query else path)

    def status(self) -> dict:
        return self._request("GET", "/api/status/")

    def device_type_id(self, model: str) -> int | None:
        found = self.get("/api/dcim/device-types/", model=model, limit=2)
        results = found.get("results", [])
        if len(results) != 1:
            return None
        return results[0]["id"]

    def existing_names(self, endpoint: str, device_type_id: int) -> set[str]:
        """Template names already defined on this device type.

        The server-side filter is sent only as a hint and every record is re-checked locally.
        This NetBox silently ignores unrecognised query parameters rather than rejecting them, so
        a filter name that does not match would return every device type's templates and make the
        caller skip real work as though it were already done.
        """
        names: set[str] = set()
        page = self.get(f"/api/dcim/{endpoint}/", device_type_id=device_type_id, limit=500)
        while True:
            names.update(
                item["name"]
                for item in page.get("results", [])
                if self.owned_by(item, device_type_id)
            )
            nxt = page.get("next")
            if not nxt:
                return names
            page = self._request("GET", nxt[len(self.base) :])

    @staticmethod
    def owned_by(item: dict, device_type_id: int) -> bool:
        # A component template belongs to either a device type or a module type; only the former
        # is ever relevant here, and module-type templates carry a null device_type.
        owner = item.get("device_type") or {}
        return owner.get("id") == device_type_id

    def create(self, endpoint: str, payload: dict, label: str) -> int | None:
        if not self.apply:
            self.created += 1
            return None
        result = self._request("POST", f"/api/dcim/{endpoint}/", payload)
        self.created += 1
        return result["id"]


def seed_switches(nb: NetBox, naming: str, verbose: bool) -> None:
    pattern = NAMING_SCHEMES[naming]
    for model, groups in SWITCH_INTERFACES.items():
        dt_id = nb.device_type_id(model)
        if dt_id is None:
            print(f"  SKIP {model}: not found in NetBox (or matched more than once)")
            continue
        existing = nb.existing_names("interface-templates", dt_id)
        planned: list[tuple[str, str]] = []
        index = 1
        for count, iface_type in groups:
            for _ in range(count):
                planned.append((pattern.format(n=index), iface_type))
                index += 1
        todo = [(n, t) for n, t in planned if n not in existing]
        for name, iface_type in todo:
            nb.create(
                "interface-templates",
                {"device_type": dt_id, "name": name, "type": iface_type},
                f"{model}/{name}",
            )
        nb.skipped += len(planned) - len(todo)
        for name, iface_type, mgmt_only in MGMT_LOOPBACK[naming]:
            if name in existing:
                nb.skipped += 1
                continue
            payload = {
                "device_type": dt_id,
                "name": name,
                "type": iface_type,
            }
            if mgmt_only:
                payload["mgmt_only"] = True
            nb.create("interface-templates", payload, f"{model}/{name}")
            existing.add(name)
        summary = ", ".join(f"{c}x {t}" for c, t in groups)
        print(
            f"  {model:<34} {len(todo):>3} to create, "
            f"{len(planned) - len(todo):>3} present  [{summary}]"
        )
        if verbose and todo:
            print(f"      {todo[0][0]} .. {todo[-1][0]}")


def seed_panels(nb: NetBox, verbose: bool) -> None:
    for panel in build_panels():
        dt_id = nb.device_type_id(panel.model)
        if dt_id is None:
            print(f"  SKIP {panel.model}: not found in NetBox (or matched more than once)")
            continue

        existing_rear = nb.existing_names("rear-port-templates", dt_id)
        existing_front = nb.existing_names("front-port-templates", dt_id)

        rear_ids: dict[str, int | None] = {}
        rear_todo = [r for r in panel.rear if r["name"] not in existing_rear]
        for spec in rear_todo:
            rear_ids[spec["name"]] = nb.create(
                "rear-port-templates",
                {
                    "device_type": dt_id,
                    "name": spec["name"],
                    "type": spec["type"],
                    "positions": spec["positions"],
                },
                f"{panel.model}/rear/{spec['name']}",
            )
        nb.skipped += len(panel.rear) - len(rear_todo)

        # Front ports carry the mapping, so they need the rear port ids. Re-read to pick up both
        # the ones just created and any that already existed. Ownership is re-checked locally for
        # the same reason as in existing_names: mapping a front port to another device type's rear
        # port would be silently wrong.
        if nb.apply:
            for item in nb.get(
                "/api/dcim/rear-port-templates/", device_type_id=dt_id, limit=500
            ).get("results", []):
                if nb.owned_by(item, dt_id):
                    rear_ids[item["name"]] = item["id"]

        front_todo = [f for f in panel.front if f["name"] not in existing_front]
        for spec in front_todo:
            payload: dict[str, Any] = {
                "device_type": dt_id,
                "name": spec["name"],
                "type": spec["type"],
                "positions": spec["positions"],
            }
            rear_id = rear_ids.get(spec["rear_name"])
            if rear_id is not None:
                payload["rear_ports"] = [
                    {
                        "position": 1,
                        "rear_port": rear_id,
                        "rear_port_position": spec["rear_position"],
                    }
                ]
            elif nb.apply:
                raise SystemExit(
                    f"{panel.model}: front port {spec['name']} needs rear port "
                    f"{spec['rear_name']}, which was neither found nor created"
                )
            nb.create("front-port-templates", payload, f"{panel.model}/front/{spec['name']}")
        nb.skipped += len(panel.front) - len(front_todo)

        print(
            f"  {panel.model:<34} rear {len(rear_todo):>3}/{len(panel.rear):<3} "
            f"front {len(front_todo):>3}/{len(panel.front):<3}  — {panel.description}"
        )
        if verbose and panel.front:
            sample = panel.front[0]
            print(
                f"      e.g. front {sample['name']} -> rear {sample['rear_name']} "
                f"position {sample['rear_position']}"
            )


def load_env() -> tuple[str, str]:
    url = os.environ.get("NETBOX_URL")
    token = os.environ.get("NETBOX_TOKEN")
    if not (url and token):
        env_file = REPO_ROOT / ".env"
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key == "NETBOX_URL" and not url:
                    url = value.strip()
                elif key == "NETBOX_TOKEN" and not token:
                    token = value.strip()
    if not url or not token:
        raise SystemExit("NETBOX_URL and NETBOX_TOKEN must be set in the environment or .env")
    return url, token


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--apply", action="store_true", help="actually write (default is a dry run)"
    )
    parser.add_argument("--naming", choices=sorted(NAMING_SCHEMES), default="cumulus")
    parser.add_argument("--allow-remote", action="store_true", help="permit a non-loopback target")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    url, token = load_env()
    host = (urlparse(url).hostname or "").lower()
    if host not in LOOPBACK_HOSTS and not args.allow_remote:
        raise SystemExit(
            f"Refusing to touch {host}: not a loopback host. This script writes to NetBox.\n"
            "Pass --allow-remote only if you genuinely mean to seed a shared instance."
        )

    nb = NetBox(url, token, apply=args.apply)
    version = nb.status().get("netbox-version", "unknown")
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"{mode} against {host} (NetBox {version}), interface naming: {args.naming}\n")

    print("Switch interface templates:")
    seed_switches(nb, args.naming, args.verbose)
    print("\nPatch panel front/rear port templates:")
    seed_panels(nb, args.verbose)

    verb = "created" if args.apply else "would create"
    print(f"\n{verb}: {nb.created}   already present: {nb.skipped}")
    if not args.apply:
        print("\nDry run only — nothing was written. Re-run with --apply to commit.")
    else:
        print("\nDone. Devices created from these types will now inherit their ports.")
        print("Reminder: devices that already existed were NOT retro-fitted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
