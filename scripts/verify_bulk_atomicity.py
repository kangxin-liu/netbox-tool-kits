#!/usr/bin/env python3
# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Measure whether a NetBox bulk create (POST a list to one endpoint) is atomic.

WHY THIS EXISTS
---------------
NetBox documents bulk *update* and bulk *delete* as all-or-none but says nothing about bulk
*create*. The answer decides the apply strategy in `design.md`: if a list POST is transactional,
apply can group creates by endpoint and the half-complete window shrinks from "between any two
rows" to "between two endpoints". Decision D7 in the spec rests on this measurement, so the
measurement needs to be repeatable — re-run it after a NetBox upgrade rather than trusting a
result recorded against an older version.

Result as of NetBox 4.6.8 (2026-08-18): atomic, all three failure classes rolled back.

WHAT IT TESTS
-------------
Three failure classes, because they fail at different points in the request and could plausibly
behave differently:

  A  field-level    a required field missing on one item. DRF validates the whole list before
                    saving anything, so this passing proves nothing about transactions on its own.
  B  deferred DB    two items in one list sharing a unique value. Neither exists at validation
                    time, so both pass; the second then violates a database constraint *after the
                    first has been inserted*. This is the test that actually settles the question.
  C  model clean()  two devices claiming the same rack unit — NetBox's own business rules, and the
                    failure most likely to hit the device importer in practice.

Plus a control: an all-valid list must create every item, otherwise the tests above prove nothing.

The script also reports the SHAPE of each error body. NetBox returns per-item arrays for some
failures (positionally aligned to the submitted list, so an error maps to a spreadsheet line) and a
single un-indexed object for others (no way to tell which row was at fault). That distinction
drives the row-by-row replay fallback in the apply pipeline.

SAFETY
------
* Dry run by default. Nothing is written without --apply.
* Refuses any target that is not a loopback host unless --allow-remote is passed, mirroring the
  loopback-only exception in the spec (requirement 2.3a).
* Every object it creates is named with the ZZ-D7- prefix, tracked by id, and deleted in a finally
  block. It also sweeps the prefix before and after, so a crashed run cannot leave residue behind.
* It creates only tenants, one scratch rack, and devices in that rack. It never touches anything
  it did not create, and it deletes nothing that lacks the prefix.
* It does leave NetBox changelog entries, which cannot be suppressed. On a shared instance that is
  a visible, attributable trace — another reason to keep this on a lab box.

USAGE
-----
    python3 scripts/verify_bulk_atomicity.py            # dry run, shows the plan
    python3 scripts/verify_bulk_atomicity.py --apply    # run the experiment
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
PREFIX = "ZZ-D7-"

# Deletion order: dependents before their dependencies, or NetBox refuses.
CLEANUP_ORDER = ["dcim/devices", "dcim/racks", "tenancy/tenants"]


class NetBox:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token
        self.created: list[tuple[str, int]] = []

    def request(self, method: str, path: str, payload: Any = None) -> tuple[int, Any]:
        """Return (status, body). Unlike the seed script, HTTP errors are values, not exits:
        provoking them is the entire point of this experiment."""
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
                body = response.read().decode()
                return response.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                # Deliberately does not echo request headers: they carry the API token.
                return exc.code, raw[:400]
        except urllib.error.URLError as exc:
            raise SystemExit(f"cannot reach {self.base}: {exc}") from None

    def get(self, path: str, **params: Any) -> Any:
        query = urllib.parse.urlencode(params)
        status, body = self.request("GET", f"{path}?{query}" if query else path)
        if status != 200:
            raise SystemExit(f"NetBox {status} on GET {path}")
        return body

    def count(self, endpoint: str) -> int:
        return self.get(f"/api/{endpoint}/", limit=1)["count"]

    def find_prefixed(self, endpoint: str) -> list[dict]:
        """Objects on this endpoint carrying the experiment's prefix.

        The `q` filter is a hint only; every record is re-checked locally, because NetBox ignores
        query parameters it does not recognise rather than rejecting them, and a filter that
        silently matched everything would turn cleanup into a wipe.
        """
        found = self.get(f"/api/{endpoint}/", q=PREFIX, limit=200)
        return [r for r in found["results"] if str(r.get("name", "")).startswith(PREFIX)]

    def post(self, endpoint: str, payload: Any) -> tuple[int, Any]:
        status, body = self.request("POST", f"/api/{endpoint}/", payload)
        if status == 201:
            for item in body if isinstance(body, list) else [body]:
                self.created.append((endpoint, item["id"]))
        return status, body

    def sweep(self, verbose: bool = True) -> int:
        """Delete every prefixed object, whether this run made it or an earlier crash left it."""
        removed = 0
        for endpoint in CLEANUP_ORDER:
            for obj in self.find_prefixed(endpoint):
                status, _ = self.request("DELETE", f"/api/{endpoint}/{obj['id']}/")
                removed += 1
                if verbose:
                    print(f"   delete {endpoint}/{obj['id']} {obj['name']}: HTTP {status}")
        return removed


def error_shape(body: Any) -> str:
    """Whether an error can be attributed to a specific submitted item."""
    if isinstance(body, list):
        return "per-item array (maps to a spreadsheet line)"
    return "single un-indexed object (row-by-row replay needed to attribute it)"


def report(name: str, status: int, body: Any, survivors: list[str]) -> bool:
    print(f"   response : HTTP {status}  {json.dumps(body)[:150] if body else ''}")
    print(f"   shape    : {error_shape(body)}")
    if survivors:
        print(f"   VERDICT  : NOT ATOMIC — these survived a failed batch: {survivors}")
        return False
    print("   VERDICT  : atomic — nothing survived")
    return True


def scratch_device_type(nb: NetBox) -> tuple[int, int, int] | None:
    """Ids for (site, 1U device type, role), or None if the instance cannot support test C."""
    sites = nb.get("/api/dcim/sites/", limit=1)["results"]
    roles = nb.get("/api/dcim/device-roles/", limit=1)["results"]
    types = [t for t in nb.get("/api/dcim/device-types/", limit=50)["results"] if t.get("u_height")]
    if not (sites and roles and types):
        return None
    return sites[0]["id"], types[0]["id"], roles[0]["id"]


def run_experiment(nb: NetBox) -> None:
    print(
        f"baseline : tenants={nb.count('tenancy/tenants')} racks={nb.count('dcim/racks')} "
        f"devices={nb.count('dcim/devices')}\n"
    )

    verdicts = []

    print("TEST A  field-level error: item 2 of 3 is missing a required 'name'")
    status, body = nb.post(
        "tenancy/tenants",
        [
            {"name": f"{PREFIX}A1", "slug": "zz-d7-a1"},
            {"slug": "zz-d7-a2"},
            {"name": f"{PREFIX}A3", "slug": "zz-d7-a3"},
        ],
    )
    verdicts.append(
        report("A", status, body, [t["name"] for t in nb.find_prefixed("tenancy/tenants")])
    )

    print("\nTEST B  deferred DB constraint: two items in one list share a slug")
    status, body = nb.post(
        "tenancy/tenants",
        [
            {"name": f"{PREFIX}B1", "slug": "zz-d7-dup"},
            {"name": f"{PREFIX}B2", "slug": "zz-d7-dup"},
        ],
    )
    verdicts.append(
        report("B", status, body, [t["name"] for t in nb.find_prefixed("tenancy/tenants")])
    )

    ids = scratch_device_type(nb)
    if ids is None:
        print("\nTEST C  SKIPPED — needs at least one site, one device role, and one device type")
        print("        with a u_height. Seed the instance first.")
        return
    site_id, type_id, role_id = ids

    print("\nTEST C  model clean() error: two devices claim the same rack unit")
    status, rack = nb.post(
        "dcim/racks",
        {
            "name": f"{PREFIX}RACK",
            "site": site_id,
            "u_height": 45,
            "status": "active",
        },
    )
    if status != 201:
        raise SystemExit(f"could not create the scratch rack: HTTP {status} {rack}")
    rack_id = rack["id"]

    def device(name: str, position: int) -> dict:
        return {
            "name": f"{PREFIX}{name}",
            "device_type": type_id,
            "role": role_id,
            "site": site_id,
            "rack": rack_id,
            "position": position,
            "face": "front",
            "status": "active",
        }

    status, body = nb.post("dcim/devices", [device("C1", 10), device("C2", 10)])
    verdicts.append(
        report("C", status, body, [d["name"] for d in nb.find_prefixed("dcim/devices")])
    )

    print("\nCONTROL  an all-valid list of two devices must create both")
    status, _ = nb.post("dcim/devices", [device("OK1", 20), device("OK2", 22)])
    made = sorted(d["name"] for d in nb.find_prefixed("dcim/devices"))
    control_ok = status == 201 and len(made) == 2
    print(f"   response : HTTP {status}, created {made}")
    verdict = "as expected" if control_ok else "UNEXPECTED — the tests above prove nothing"
    print(f"   VERDICT  : {verdict}")

    print("\n" + "=" * 78)
    if not control_ok:
        print(
            "INCONCLUSIVE: the control failed, so the rollbacks above "
            "may be unrelated to atomicity."
        )
    elif all(verdicts):
        print("BULK CREATE IS ATOMIC: every failure class rolled the whole list back.")
        print("Apply may group creates by endpoint and send one bulk POST each.")
    else:
        print("BULK CREATE IS NOT ATOMIC: a failed batch left objects behind.")
        print("Apply must stay row-by-row with ordered audit records.")
    print("=" * 78)


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
        "--apply", action="store_true", help="actually run it (default is a dry run)"
    )
    parser.add_argument("--allow-remote", action="store_true", help="permit a non-loopback target")
    args = parser.parse_args()

    url, token = load_env()
    host = (urlparse(url).hostname or "").lower()
    if host not in LOOPBACK_HOSTS and not args.allow_remote:
        raise SystemExit(
            f"Refusing to touch {host}: not a loopback host. This script writes to NetBox.\n"
            "Pass --allow-remote only if you genuinely mean to experiment on a shared instance."
        )

    nb = NetBox(url, token)
    version = nb.get("/api/status/").get("netbox-version", "unknown")

    if not args.apply:
        print(f"DRY RUN against {host} (NetBox {version}). Nothing will be written.\n")
        print("With --apply this would:")
        print(f"  1. sweep any leftover {PREFIX}* objects")
        print("  2. POST 3 tenants, one invalid          (test A: field-level error)")
        print("  3. POST 2 tenants with a duplicate slug (test B: deferred DB constraint)")
        print(f"  4. POST 1 rack named {PREFIX}RACK into the first site")
        print("  5. POST 2 devices at the same position  (test C: model clean() error)")
        print("  6. POST 2 valid devices                 (control)")
        print(f"  7. delete every {PREFIX}* object it created and re-check the counts")
        print("\nRe-run with --apply to measure.")
        return 0

    print(f"APPLY against {host} (NetBox {version})\n")
    leftovers = nb.sweep(verbose=False)
    if leftovers:
        print(f"cleared {leftovers} leftover {PREFIX}* object(s) from an earlier run\n")

    before = {e: nb.count(e) for e in CLEANUP_ORDER}
    try:
        run_experiment(nb)
    finally:
        print("\nCLEANUP")
        nb.sweep()
        after = {e: nb.count(e) for e in CLEANUP_ORDER}
        print(f"\ncounts before: {before}")
        print(f"counts after : {after}")
        if before != after:
            print("\nWARNING: counts do not match. Inspect the instance for residue.")
        else:
            print("\nInstance is back to its starting state (changelog entries excepted).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
