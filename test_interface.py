
#!/usr/bin/env python3

import os
import json
import argparse
from collections import defaultdict

import pynetbox
import requests


# ==========================================================
# NetBox connection
# ==========================================================

NETBOX_URL = os.getenv(
    "NETBOX_URL",
    "https://netbox.example.com"
)

NETBOX_TOKEN = os.getenv("NETBOX_TOKEN")

if not NETBOX_TOKEN:
    raise RuntimeError(
        "Please set the NETBOX_TOKEN environment variable"
    )


def connect_netbox():
    nb = pynetbox.api(
        NETBOX_URL.rstrip("/"),
        token=NETBOX_TOKEN
    )

    # Use certificate verification in production.
    session = requests.Session()
    session.verify = True

    nb.http_session = session

    return nb


# ==========================================================
# Helpers
# ==========================================================

def get_id(obj):
    """Return ID from a nested NetBox object."""
    if obj is None:
        return None

    if isinstance(obj, int):
        return obj

    if isinstance(obj, dict):
        return obj.get("id")

    return getattr(obj, "id", None)


def get_value(obj):
    """Convert NetBox choice fields into readable values."""
    if obj is None:
        return None

    if isinstance(obj, dict):
        return obj.get("value", obj.get("label"))

    return getattr(obj, "value", str(obj))


def get_ip_details(ip):
    """Extract IP address details."""
    return {
        "id": ip.id,
        "address": ip.address,
        "vrf": (
            str(ip.vrf)
            if getattr(ip, "vrf", None)
            else None
        ),
        "dns_name": getattr(ip, "dns_name", ""),
        "status": get_value(
            getattr(ip, "status", None)
        ),
    }


def get_interface_details(interface):
    """Extract useful fields from an interface."""
    return {
        "id": interface.id,
        "name": interface.name,
        "type": get_value(
            getattr(interface, "type", None)
        ),
        "enabled": getattr(interface, "enabled", None),
        "description": getattr(
            interface, "description", ""
        ),
        "mtu": getattr(interface, "mtu", None),
        "parent_id": get_id(
            getattr(interface, "parent", None)
        ),
        "lag_id": get_id(
            getattr(interface, "lag", None)
        ),
        "ip_addresses": [],
        "sub_interfaces": [],
    }


# ==========================================================
# Main query
# ==========================================================

def get_device_interfaces(nb, device_name):

    # 1. Find device
    device = nb.dcim.devices.get(name=device_name)

    if device is None:
        raise ValueError(
            f"Device '{device_name}' not found"
        )

    print(
        f"Found device: {device.name} (ID: {device.id})"
    )

    # 2. Retrieve all device interfaces
    interfaces = list(
        nb.dcim.interfaces.filter(device_id=device.id)
    )

    print(
        f"Total interfaces: {len(interfaces)}"
    )

    # 3. Retrieve all assigned IP addresses
    ip_addresses = list(
        nb.ipam.ip_addresses.filter(device_id=device.id)
    )

    print(
        f"Total assigned IP addresses: {len(ip_addresses)}"
    )

    # 4. Build interface ID -> IP address mapping
    ip_map = defaultdict(list)

    for ip in ip_addresses:

        if getattr(
            ip, "assigned_object_type", None
        ) != "dcim.interface":
            continue

        interface_id = get_id(
            getattr(ip, "assigned_object_id", None)
        )

        if interface_id is not None:
            ip_map[interface_id].append(
                get_ip_details(ip)
            )

    # 5. Build interface objects
    interface_map = {}

    for interface in interfaces:

        data = get_interface_details(interface)

        data["ip_addresses"] = ip_map.get(
            interface.id, []
        )

        interface_map[interface.id] = data

    # 6. Build parent-child relationships
    root_interfaces = []

    for interface in interfaces:

        data = interface_map[interface.id]
        parent_id = data["parent_id"]

        if (
            parent_id is not None
            and parent_id in interface_map
            and parent_id != interface.id
        ):
            interface_map[parent_id][
                "sub_interfaces"
            ].append(data)
        else:
            root_interfaces.append(data)

    # 7. Sort interfaces recursively
    def sort_interfaces(items):
        items.sort(key=lambda x: x["name"])

        for item in items:
            sort_interfaces(item["sub_interfaces"])

    sort_interfaces(root_interfaces)

    # 8. Return structured results
    return {
        "device": {
            "id": device.id,
            "name": device.name,
        },
        "interface_count": len(interfaces),
        "ip_address_count": len(ip_addresses),
        "interfaces": root_interfaces,
    }


# ==========================================================
# Pretty printer
# ==========================================================

def print_interface(interface, indent=0):

    prefix = " " * indent

    print(
        f"{prefix}Interface: {interface['name']}"
    )
    print(
        f"{prefix}  ID: {interface['id']}"
    )
    print(
        f"{prefix}  Type: {interface['type']}"
    )
    print(
        f"{prefix}  Enabled: {interface['enabled']}"
    )

    if interface["description"]:
        print(
            f"{prefix}  Description: "
            f"{interface['description']}"
        )

    if interface["parent_id"] is not None:
        print(
            f"{prefix}  Parent ID: "
            f"{interface['parent_id']}"
        )

    if interface["ip_addresses"]:
        print(f"{prefix}  IP addresses:")

        for ip in interface["ip_addresses"]:
            print(
                f"{prefix}    - {ip['address']}"
                f" (VRF: {ip['vrf'] or 'Global'})"
            )
    else:
        print(f"{prefix}  IP addresses: None")

    # Recursively print child interfaces
    for child in interface["sub_interfaces"]:
        print_interface(child, indent + 4)


# ==========================================================
# Entry point
# ==========================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Retrieve NetBox interfaces, sub-interfaces "
            "and their IP addresses"
        )
    )

    parser.add_argument(
        "device",
        help="Exact NetBox device name"
    )

    parser.add_argument(
        "--json",
        dest="json_file",
        help="Optional JSON output filename"
    )

    args = parser.parse_args()

    try:
        nb = connect_netbox()

        result = get_device_interfaces(
            nb, args.device
        )

        print("\n========== INTERFACES ==========\n")

        for interface in result["interfaces"]:
            print_interface(interface)
            print()

        if args.json_file:
            with open(
                args.json_file,
                "w",
                encoding="utf-8"
            ) as f:
                json.dump(
                    result,
                    f,
                    indent=2,
                    ensure_ascii=False
                )

            print(
                f"JSON exported to: {args.json_file}"
            )

    except (requests.RequestException, ValueError) as e:
        print(f"Error: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

