#!/usr/bin/env python3
# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Pin down what an interface created from a template actually looks like (task 31a, D12).

WHY THIS EXISTS
---------------
Requirement 9.25 promises that an interface the cabling sheet creates is "the one NetBox would have
produced had the template existed when the device was created". That is a claim about field
fidelity, and `importers/cabling.py` implements it by copying a fixed list of template fields onto
the interface payload. Nothing in the test suite can check the list is right, because the double
only knows the fields the double was taught. Four things need a real instance:

  1. **Which template fields does `dcim.Interface` accept verbatim?** The importer copies `type`,
     `duplex`, `poe_mode`, `poe_type`, `rf_role` as choice values and `label`, `description`,
     `speed`, `mgmt_only` as they are. A field NetBox names differently on the two models is copied
     as nothing today — safe, but it makes 9.25's promise false in a way nobody would notice.
  2. **Does the created interface match a template-produced one field for field?** The direct test:
     create a device from the type (NetBox does the copying), create the same interface by hand on
     another device, and diff.
  3. **Is a bulk POST to `dcim.interfaces` atomic?** Decision D7 is measured for cables, devices and
     racks. The cabling sheet now posts interfaces as a batch too, and Requirement 9.32's "the
     interface batch either happened or did not" leans on it.
  4. **Is a duplicate `(device, name)` rejected?** Requirement 9.32 aborts the run when an interface
     appears between validation and apply, on the grounds that posting it would fail. If NetBox
     quietly accepts a second interface of the same name, that abort is protecting against nothing
     and the sheet would instead create a confusing duplicate.

SAFETY
------
* **This script writes.** It creates a device, interfaces, and a device type, and deletes what it
  created. Like `probe_cable_profiles.py` it is the only kind of thing here that issues DELETE; the
  app's own HTTP layer refuses any verb outside GET/OPTIONS/POST by design.
* `--url` is **required and never defaulted**, so the instance in `.env` cannot be hit by forgetting
  an argument. The token still comes from the environment.
* `--i-know-this-is-disposable` is required. There is no way to run this without saying so.
* Everything it creates carries the `PROBE-` prefix, is tracked as it is made, and is deleted in a
  `finally`. Anything that outlives the run is printed loudly.
* It touches no device, type, or interface it did not create.

USAGE
-----
    export NETBOX_TOKEN=...          # or leave it in .env
    python3 scripts/probe_interface_creation.py --url http://localhost:8000 \
        --i-know-this-is-disposable

Exit code is non-zero if any expectation was violated, so this can gate the spec update.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

PREFIX = "PROBE-IFACE"
TEMPLATE_NAME = "probeEth0"

# What the importer copies from a template (cabling.TEMPLATE_CHOICE_FIELDS / TEMPLATE_PLAIN_FIELDS).
# Kept as a literal rather than imported, so the probe measures the API and the app's idea of the
# API independently — importing would make them agree by construction.
CHOICE_FIELDS = ("type", "duplex", "poe_mode", "poe_type", "rf_role")
PLAIN_FIELDS = ("label", "description", "speed", "mgmt_only")

# Fields that legitimately differ between two interfaces on two devices, so a diff that reports
# them is noise rather than a finding.
NOT_COMPARABLE = {
    "id",
    "url",
    "display",
    "display_url",
    "device",
    "created",
    "last_updated",
    "cable",
    "cable_end",
    "link_peers",
    "link_peers_type",
    "connected_endpoints",
    "connected_endpoints_type",
    "connected_endpoints_reachable",
    "_occupied",
    "count_ipaddresses",
    "count_fhrp_groups",
}


class NetBox:
    """Minimal client. Separate from the app's on purpose — this one has to be able to DELETE."""

    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self._token = token

    def _request(self, method: str, path: str, payload: Any = None) -> tuple[int, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Token {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = None if response.status == 204 else json.load(response)
                return response.status, body
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                body = json.loads(raw)
            except ValueError:
                # Never echo request headers: they carry the token.
                body = raw[:400]
            return exc.code, body

    def get(self, path: str, **params: Any) -> Any:
        query = urllib.parse.urlencode(params, doseq=True)
        status, body = self._request("GET", f"{path}?{query}" if query else path)
        if status != 200:
            raise SystemExit(f"GET {path} returned {status}: {body}")
        return body

    def options(self, path: str) -> Any:
        status, body = self._request("OPTIONS", path)
        if status != 200:
            raise SystemExit(f"OPTIONS {path} returned {status}: {body}")
        return body

    def post(self, path: str, payload: Any) -> tuple[int, Any]:
        return self._request("POST", path, payload)

    def delete(self, path: str) -> int:
        status, _ = self._request("DELETE", path)
        return status


@dataclass
class Finding:
    name: str
    question: str
    answer: str
    expected: str = ""
    ok: bool = True
    detail: list[str] = field(default_factory=list)


def load_token() -> str:
    """Token from the environment, or from `.env` if that is where it lives. Never from an argument,
    so it cannot end up in a shell history or a process listing."""
    token = os.environ.get("NETBOX_TOKEN", "").strip()
    if token:
        return token
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "NETBOX_TOKEN":
                return value.strip().strip("'\"")
    raise SystemExit("NETBOX_TOKEN is not set, and .env does not carry one.")


def value_of(raw: Any) -> Any:
    """A choice field comes back as `{value, label}` and goes out as the bare value."""
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    if isinstance(raw, dict) and "id" in raw:
        return raw["id"]
    return raw


class Probe:
    def __init__(self, nb: NetBox) -> None:
        self.nb = nb
        self.findings: list[Finding] = []
        # (endpoint path, id), deleted in reverse order so children go before parents.
        self.created: list[tuple[str, int]] = []

    def record(self, path: str, record: dict[str, Any]) -> dict[str, Any]:
        self.created.append((path, int(record["id"])))
        return record

    def must_post(self, path: str, payload: Any) -> Any:
        status, body = self.nb.post(path, payload)
        if status not in (200, 201):
            raise SystemExit(f"POST {path} returned {status}: {body}")
        if isinstance(body, list):
            for item in body:
                self.record(path, item)
        else:
            self.record(path, body)
        return body

    # --- fixture ----------------------------------------------------------------------------

    def build(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """A throwaway type carrying one richly populated template, and two devices from it.

        `inherited` is created *after* the template, so NetBox copies the template onto it — that
        is the reference the hand-made interface is compared against. `bare` is created before its
        own template exists, which is precisely the state Requirement 9.25 exists to repair.
        """
        manufacturer = self.must_post(
            "/api/dcim/manufacturers/", {"name": f"{PREFIX}-mfr", "slug": f"{PREFIX.lower()}-mfr"}
        )
        device_type = self.must_post(
            "/api/dcim/device-types/",
            {
                "manufacturer": manufacturer["id"],
                "model": f"{PREFIX}-type",
                "slug": f"{PREFIX.lower()}-type",
                "u_height": 1,
            },
        )
        role = self.must_post(
            "/api/dcim/device-roles/", {"name": f"{PREFIX}-role", "slug": f"{PREFIX.lower()}-role"}
        )
        site = self.must_post(
            "/api/dcim/sites/", {"name": f"{PREFIX}-site", "slug": f"{PREFIX.lower()}-site"}
        )

        # Created before the template, so it does NOT have the interface. The D12 case.
        bare = self.must_post(
            "/api/dcim/devices/",
            {
                "name": f"{PREFIX}-bare",
                "device_type": device_type["id"],
                "role": role["id"],
                "site": site["id"],
            },
        )

        template = self.must_post(
            "/api/dcim/interface-templates/",
            {
                "device_type": device_type["id"],
                "name": TEMPLATE_NAME,
                "type": "1000base-t",
                "mgmt_only": True,
                "label": "probe-label",
                "description": "probe description",
                # Left out deliberately where NetBox may reject them on a 1000base-t: the point is
                # to learn which of the candidate fields a template even carries.
            },
        )

        # Created after the template, so NetBox copies it on. The reference.
        inherited = self.must_post(
            "/api/dcim/devices/",
            {
                "name": f"{PREFIX}-inherited",
                "device_type": device_type["id"],
                "role": role["id"],
                "site": site["id"],
            },
        )
        return bare, inherited, template

    # --- the questions ----------------------------------------------------------------------

    def template_fields(self, template: dict[str, Any]) -> None:
        """Q1: which of the fields the importer copies does a template actually carry?"""
        carried = [name for name in CHOICE_FIELDS + PLAIN_FIELDS if name in template]
        missing = [name for name in CHOICE_FIELDS + PLAIN_FIELDS if name not in template]
        accepted = set(self.nb.options("/api/dcim/interfaces/")["actions"]["POST"])
        unusable = [name for name in carried if name not in accepted]

        self.findings.append(
            Finding(
                name="template fields the interface endpoint accepts",
                question="Which of the fields the importer copies exist on both models?",
                answer=f"{len(carried)} carried by the template, {len(unusable)} not POSTable",
                expected="every carried field is accepted by POST /api/dcim/interfaces/",
                ok=not unusable,
                detail=[
                    f"carried by the template: {', '.join(carried) or 'none'}",
                    f"absent from the template: {', '.join(missing) or 'none'}",
                    f"carried but NOT accepted on POST: {', '.join(unusable) or 'none'}",
                    "-> if anything is listed on the line above, cabling.py copies a field NetBox",
                    "   will reject; if 'absent' lists something, 9.25 promises a field that does",
                    "   not exist and the list in cabling.py should lose it.",
                ],
            )
        )

    def fidelity(self, bare: dict[str, Any], inherited: dict[str, Any]) -> None:
        """Q2: does a hand-made interface match the one the template produced?"""
        reference = self.nb.get(
            "/api/dcim/interfaces/", device_id=inherited["id"], name=TEMPLATE_NAME
        )["results"][0]

        template = self.nb.get("/api/dcim/interface-templates/", name=TEMPLATE_NAME)["results"][0]
        payload: dict[str, Any] = {"device": bare["id"], "name": TEMPLATE_NAME}
        for name in CHOICE_FIELDS:
            value = value_of(template.get(name))
            if value not in (None, ""):
                payload[name] = value
        for name in PLAIN_FIELDS:
            value = template.get(name)
            if value not in (None, ""):
                payload[name] = value

        made = self.must_post("/api/dcim/interfaces/", payload)

        differences = [
            f"{name}: template-produced {value_of(reference.get(name))!r} vs "
            f"hand-made {value_of(made.get(name))!r}"
            for name in sorted(set(reference) | set(made))
            if name not in NOT_COMPARABLE
            and value_of(reference.get(name)) != value_of(made.get(name))
        ]
        self.findings.append(
            Finding(
                name="field fidelity against a template-produced interface",
                question="Is the interface we create the one the device type would have produced?",
                answer="identical" if not differences else f"{len(differences)} field(s) differ",
                expected="identical on every comparable field (Requirement 9.25)",
                ok=not differences,
                detail=differences or [f"payload sent: {json.dumps(payload, sort_keys=True)}"],
            )
        )

    def duplicate_name(self, bare: dict[str, Any]) -> None:
        """Q4: is a second interface of the same name on one device refused?"""
        status, body = self.nb.post(
            "/api/dcim/interfaces/",
            {"device": bare["id"], "name": TEMPLATE_NAME, "type": "1000base-t"},
        )
        if status in (200, 201):
            record = body[0] if isinstance(body, list) else body
            self.record("/api/dcim/interfaces/", record)
        self.findings.append(
            Finding(
                name="duplicate (device, name)",
                question="Does NetBox refuse a second interface of the same name on one device?",
                answer=f"HTTP {status}",
                expected="4xx — Requirement 9.32 aborts the run on the assumption that it fails",
                ok=400 <= status < 500,
                detail=[f"body: {json.dumps(body)[:300]}"],
            )
        )

    def batch_atomicity(self, bare: dict[str, Any]) -> None:
        """Q3: does a rejected list POST leave nothing behind (decision D7)?"""
        before = self.nb.get("/api/dcim/interfaces/", device_id=bare["id"], limit=1)["count"]
        status, body = self.nb.post(
            "/api/dcim/interfaces/",
            [
                {"device": bare["id"], "name": f"{PREFIX}-good", "type": "1000base-t"},
                # Rejected: a type NetBox does not know. The first item is perfectly valid, so if
                # the batch is not atomic it survives.
                {"device": bare["id"], "name": f"{PREFIX}-bad", "type": "wet-string"},
            ],
        )
        after = self.nb.get("/api/dcim/interfaces/", device_id=bare["id"], limit=1)["count"]
        if status in (200, 201):
            for item in body if isinstance(body, list) else [body]:
                self.record("/api/dcim/interfaces/", item)

        self.findings.append(
            Finding(
                name="bulk interface POST atomicity",
                question="Does a rejected interface batch roll the whole list back?",
                answer=f"HTTP {status}, interface count {before} -> {after}",
                expected="4xx and an unchanged count (decision D7, as measured for cables)",
                ok=400 <= status < 500 and before == after,
                detail=[f"body: {json.dumps(body)[:300]}"],
            )
        )

    # --- cleanup ----------------------------------------------------------------------------

    def cleanup(self) -> list[tuple[str, int]]:
        """Delete everything created, children first. Returns whatever would not go."""
        stubborn: list[tuple[str, int]] = []
        for path, object_id in reversed(self.created):
            status = self.nb.delete(f"{path}{object_id}/")
            if status not in (204, 404):
                stubborn.append((path, object_id))
        self.created = []
        return stubborn


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Probe how NetBox creates an interface from a template (task 31a). "
            "Creates and deletes a throwaway device type, devices and interfaces."
        )
    )
    parser.add_argument(
        "--url",
        required=True,
        help="target NetBox, e.g. http://localhost:8000. Never defaulted: this script writes.",
    )
    parser.add_argument(
        "--i-know-this-is-disposable",
        action="store_true",
        help="required. Confirms the target is an instance you are willing to have written to.",
    )
    args = parser.parse_args()

    if not args.i_know_this_is_disposable:
        print(
            "Refusing to run: this script creates and deletes devices and interfaces. Pass\n"
            "  --i-know-this-is-disposable\n"
            "once you are pointing at an instance you are willing to have written to.",
            file=sys.stderr,
        )
        return 2

    nb = NetBox(args.url, load_token())
    host = urllib.parse.urlparse(args.url).netloc
    status = nb.get("/api/status/")
    version = status.get("netbox-version", "?")
    print(f"target {host}, NetBox {version}")
    if not version.startswith("4.6"):
        print(f"  warning: findings apply to 4.6.x; this is {version}")

    existing = nb.get("/api/dcim/device-types/", slug=f"{PREFIX.lower()}-type", limit=1)
    if existing.get("count"):
        print(
            f"\nRefusing to run: a device type slugged {PREFIX.lower()}-type already exists, so a "
            "previous run did not clean up. Remove it and its devices by hand first.",
            file=sys.stderr,
        )
        return 2

    probe = Probe(nb)
    stubborn: list[tuple[str, int]] = []
    try:
        bare, inherited, template = probe.build()
        print(f"built {PREFIX}-type with template {TEMPLATE_NAME}, two devices\n")
        probe.template_fields(template)
        probe.fidelity(bare, inherited)
        probe.duplicate_name(bare)
        probe.batch_atomicity(bare)
    finally:
        stubborn = probe.cleanup()

    print("=" * 96)
    failures = 0
    for finding in probe.findings:
        mark = "ok " if finding.ok else "!! "
        print(f"\n{mark}{finding.name}")
        print(f"    Q: {finding.question}")
        print(f"    A: {finding.answer}")
        if finding.expected:
            print(f"    expected: {finding.expected}")
        for line in finding.detail:
            print(f"      {line}")
        failures += 0 if finding.ok else 1

    print("\n" + "=" * 96)
    if stubborn:
        print(f"!! could not delete {stubborn} — remove them by hand", file=sys.stderr)
        failures += 1
    else:
        print("cleanup complete: everything this script created has been deleted")

    print(
        f"\n{len(probe.findings) - failures} of {len(probe.findings)} expectations held."
        if failures
        else f"\nAll {len(probe.findings)} expectations held."
    )
    print(
        "Record the answers in design.md's D12 section and tick task 31a, amending Requirement "
        "9.25's field list and cabling.py's TEMPLATE_* tuples wherever reality disagreed."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
