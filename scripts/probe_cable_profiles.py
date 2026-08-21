#!/usr/bin/env python3
# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Answer the behavioural questions about cable profiles that OPTIONS cannot (task 23b, D10).

WHY THIS EXISTS
---------------
`OPTIONS /api/dcim/cables/` tells us `profile` exists and lists its 26 values (task 23a, verified).
It does not tell us how NetBox *behaves* with one, and four behaviours are load-bearing for the
cabling importer:

  1. Does NetBox accept fewer terminations than the profile has connectors? Requirement 9.19 checks
     `<=`; if NetBox demands `==`, a partly populated breakout stops being expressible and the check
     has to change.
  2. Does it reject more terminations than the profile has connectors? If not, our check is the only
     thing standing between an operator and a cable NetBox cannot trace.
  3. Are connectors assigned in submitted array order? Requirement 9.21 makes row order the lane
     order, which is only honest if this holds. If it does not, the plan preview lies and explicit
     `a_connector` / `b_connector` columns become mandatory.
  4. What does a `-shuffle` profile actually map? The mapping is NetBox's own and cannot be set, so
     an operator whose harness shuffles differently gets confident, wrong path traces. This prints
     the mapping so it can be compared against a harness specification.

None of it can be observed without creating a cable, which is why this is a script and not a test.

SAFETY
------
* **This script writes.** It creates cables and deletes the ones it created. That makes it the only
  thing in this repository that issues DELETE — the app's own HTTP layer refuses any verb outside
  GET/OPTIONS/POST by design, and this script deliberately does not use it.
* `--url` is **required and never defaulted**, so the instance configured in `.env` cannot be hit by
  forgetting an argument. The token still comes from the environment.
* `--i-know-this-is-disposable` is required. There is no way to run this without saying so.
* Refuses an instance that already holds cables unless `--allow-existing-cables` is passed, because
  a populated instance is not a throwaway.
* Creates no devices, interfaces, sites or racks. It uses free interfaces that already exist, and if
  there are not enough it says what is missing and stops.
* Cleans up in a `finally`, and reports loudly if anything it created outlived the run.

USAGE
-----
    export NETBOX_TOKEN=...          # or leave it in .env
    python3 scripts/probe_cable_profiles.py --url http://localhost:8000 \
        --i-know-this-is-disposable

Exit code is non-zero if any expectation was violated, so this can gate a spec update.
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

BREAKOUT = "breakout-1c4p-4c1p"
SHUFFLE_TRUNK = "trunk-4c4p-shuffle"
CABLE_TYPE = "mmf-om4"

# How many free interfaces the probes need between them: the trunk probe wants four a side.
NEEDED_INTERFACES = 9


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

    def get_all(self, path: str, **params: Any) -> list[dict[str, Any]]:
        page = self.get(path, limit=500, **params)
        records = list(page.get("results", []))
        while page.get("next"):
            page = self.get(page["next"][len(self.base) :])
            records += page.get("results", [])
        return records

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


def termination(interface_id: int) -> dict[str, Any]:
    return {"object_type": "dcim.interface", "object_id": interface_id}


def free_interfaces(nb: NetBox) -> list[dict[str, Any]]:
    """Uncabled interfaces, at most one per device, so a probe looks like real cabling.

    `cabled=false` is sent as a hint and every record is re-checked locally, because this NetBox
    ignores an unrecognised query parameter and returns everything with HTTP 200.
    """
    seen: set[int] = set()
    chosen: list[dict[str, Any]] = []
    for record in nb.get_all("/api/dcim/interfaces/", cabled="false"):
        if record.get("cable") is not None or record.get("mark_connected"):
            continue
        device_id = (record.get("device") or {}).get("id")
        if device_id is None or device_id in seen:
            continue
        seen.add(device_id)
        chosen.append(record)
        if len(chosen) >= NEEDED_INTERFACES:
            break
    return chosen


def describe(record: dict[str, Any]) -> str:
    return f"{(record.get('device') or {}).get('name')}:{record.get('name')}"


def read_terminations(nb: NetBox, cable_id: int) -> list[dict[str, Any]]:
    """Terminations of one cable, with whatever connector/position fields NetBox exposes."""
    return nb.get_all("/api/dcim/cable-terminations/", cable_id=cable_id)


def summarise(terminations: list[dict[str, Any]]) -> list[str]:
    lines = []
    for item in sorted(terminations, key=lambda t: (str(t.get("cable_end")), t.get("id") or 0)):
        obj = item.get("termination") or {}
        lines.append(
            f"end {item.get('cable_end')} "
            f"connector={item.get('connector')!r} positions={item.get('positions')!r} "
            f"-> {obj.get('display') or obj.get('name') or obj.get('id')}"
        )
    return lines


class Probe:
    def __init__(self, nb: NetBox) -> None:
        self.nb = nb
        self.created: list[int] = []
        self.findings: list[Finding] = []

    def create_cable(self, a_ids: list[int], b_ids: list[int], profile: str) -> tuple[int, Any]:
        payload = {
            "a_terminations": [termination(i) for i in a_ids],
            "b_terminations": [termination(i) for i in b_ids],
            "type": CABLE_TYPE,
            "status": "connected",
            "profile": profile,
        }
        status, body = self.nb.post("/api/dcim/cables/", payload)
        if status in (200, 201) and isinstance(body, dict) and body.get("id"):
            self.created.append(int(body["id"]))
        return status, body

    def cleanup(self) -> list[int]:
        """Delete what this run created. Returns whatever could not be removed."""
        stubborn = []
        for cable_id in reversed(self.created):
            status = self.nb.delete(f"/api/dcim/cables/{cable_id}/")
            if status not in (200, 204):
                stubborn.append(cable_id)
        self.created = [c for c in self.created if c in stubborn]
        return stubborn

    # --- the four questions -----------------------------------------------------------------

    def count_rule(self, a: dict, bs: list[dict]) -> int | None:
        """Questions 1 and 2: how many terminations may a side carry?

        Returns the id of the fully populated cable, for the connector-order probe to read back.
        """
        status, body = self.create_cable([a["id"]], [b["id"] for b in bs[:4]], BREAKOUT)
        exact_ok = status in (200, 201)
        self.findings.append(
            Finding(
                "breakout, four B terminations",
                f"does {BREAKOUT} accept its full four connectors?",
                f"HTTP {status}",
                expected="201",
                ok=exact_ok,
                detail=[] if exact_ok else [json.dumps(body)[:300]],
            )
        )
        full_id = body.get("id") if exact_ok and isinstance(body, dict) else None

        status, body = self.create_cable([a["id"]], [b["id"] for b in bs[4:5]], BREAKOUT)
        under_ok = status in (200, 201)
        self.findings.append(
            Finding(
                "breakout, one B termination",
                "may a side carry FEWER terminations than the profile has connectors?",
                "yes" if under_ok else f"no — HTTP {status}",
                expected="yes, which is what Requirement 9.19's `<=` assumes",
                ok=under_ok,
                detail=[]
                if under_ok
                else [
                    json.dumps(body)[:300],
                    "ACTION: Requirement 9.19 becomes an equality check, and a partly populated "
                    "breakout is no longer expressible. Change `_check_group` in cabling.py.",
                ],
            )
        )

        status, body = self.create_cable([a["id"]], [b["id"] for b in bs[:5]], BREAKOUT)
        over_rejected = status not in (200, 201)
        self.findings.append(
            Finding(
                "breakout, five B terminations",
                "does NetBox itself reject MORE terminations than there are connectors?",
                f"rejected with HTTP {status}" if over_rejected else "accepted (!)",
                expected="rejected",
                ok=over_rejected,
                detail=[]
                if over_rejected
                else [
                    "NetBox accepted an over-populated profile, so our S3 check is the only guard. "
                    "Keep it, and note that NetBox will not catch what we miss."
                ],
            )
        )
        return int(full_id) if full_id else None

    def connector_order(self, cable_id: int, submitted: list[dict]) -> None:
        """Question 3: is connector N the Nth termination in the submitted array?"""
        terms = read_terminations(self.nb, cable_id)
        b_side = [t for t in terms if str(t.get("cable_end")).upper() == "B"]
        by_object = {
            (t.get("termination") or {}).get("id"): t.get("connector")
            for t in b_side
            if (t.get("termination") or {})
        }
        observed = [by_object.get(record["id"]) for record in submitted]
        positional = observed == list(range(1, len(submitted) + 1))
        self.findings.append(
            Finding(
                "connector assignment order",
                "are connectors assigned in the order the terminations were submitted?",
                f"submitted {[describe(r) for r in submitted]} -> connectors {observed}",
                expected="1, 2, 3, 4 in submitted order",
                ok=positional,
                detail=summarise(terms)
                + (
                    []
                    if positional
                    else [
                        "ACTION: STOP. Requirement 9.21 makes row order the lane order and "
                        "the preview reports it; neither is true. The `a_connector` / "
                        "`b_connector` columns D10 rejected become mandatory.",
                    ]
                ),
            )
        )

    def trunk_and_shuffle(self, a_side: list[dict], b_side: list[dict]) -> None:
        """Questions 3 and 4 for a trunk: four connectors a side, and what the shuffle maps."""
        status, body = self.create_cable(
            [r["id"] for r in a_side], [r["id"] for r in b_side], SHUFFLE_TRUNK
        )
        ok = status in (200, 201)
        detail: list[str] = [] if ok else [json.dumps(body)[:300]]
        if ok and isinstance(body, dict):
            terms = read_terminations(self.nb, int(body["id"]))
            detail += summarise(terms)
            detail.append("")
            detail.append("A side, as submitted:")
            detail += [f"  connector {n}: {describe(r)}" for n, r in enumerate(a_side, start=1)]
            detail.append("B side, as submitted:")
            detail += [f"  connector {n}: {describe(r)}" for n, r in enumerate(b_side, start=1)]
            detail.append("")
            detail.append("Path trace from the first A-side interface:")
            detail += self.trace(a_side[0])
            detail.append("")
            detail.append(
                "COMPARE the positions above against the harness specification. NetBox's shuffle "
                "map cannot be configured, so a mismatch means this profile must not be used."
            )
        self.findings.append(
            Finding(
                "trunk with four terminations a side",
                f"does {SHUFFLE_TRUNK} accept 4 A + 4 B, and what does the shuffle map?",
                f"HTTP {status}",
                expected="201, with connectors 1-4 on both sides",
                ok=ok,
                detail=detail,
            )
        )

    def trace(self, interface: dict) -> list[str]:
        """Whatever NetBox reports for a per-lane trace, printed rather than interpreted."""
        status, body = self.nb._request("GET", f"/api/dcim/interfaces/{interface['id']}/trace/")
        if status != 200:
            return [f"  trace unavailable: HTTP {status}"]
        if not body:
            return ["  trace returned nothing"]
        return [f"  {json.dumps(hop)[:200]}" for hop in body]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe NetBox cable-profile behaviour (task 23b). Creates and deletes cables."
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
    parser.add_argument(
        "--allow-existing-cables",
        action="store_true",
        help="proceed even though the instance already holds cables.",
    )
    args = parser.parse_args()

    if not args.i_know_this_is_disposable:
        print(
            "Refusing to run: this script creates and deletes cables. Pass\n"
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

    cables = nb.get("/api/dcim/cables/", limit=1).get("count", 0)
    print(f"existing cables: {cables}")
    if cables and not args.allow_existing_cables:
        print(
            "\nRefusing to run: this instance already holds cables, so it is not a throwaway.\n"
            "Pass --allow-existing-cables if you are sure.",
            file=sys.stderr,
        )
        return 2

    pool = free_interfaces(nb)
    if len(pool) < NEEDED_INTERFACES:
        print(
            f"\nNeed {NEEDED_INTERFACES} uncabled interfaces on distinct devices; found "
            f"{len(pool)}. Create a few more devices with interfaces and re-run.",
            file=sys.stderr,
        )
        return 2
    print(f"using {len(pool)} free interfaces: {', '.join(describe(r) for r in pool)}\n")

    probe = Probe(nb)
    stubborn: list[int] = []
    try:
        full_id = probe.count_rule(pool[0], pool[1:6])
        if full_id:
            probe.connector_order(full_id, pool[1:5])
        probe.cleanup()
        probe.trunk_and_shuffle(pool[0:4], pool[4:8])
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
    remaining = nb.get("/api/dcim/cables/", limit=1).get("count", 0)
    print(f"cables before {cables}, after {remaining}")
    if stubborn:
        print(f"!! could not delete cables {stubborn} — remove them by hand", file=sys.stderr)
        failures += 1
    elif remaining != cables:
        print("!! cable count did not return to its starting value", file=sys.stderr)
        failures += 1

    print(
        f"\n{len(probe.findings) - failures} of {len(probe.findings)} expectations held."
        if failures
        else f"\nAll {len(probe.findings)} expectations held."
    )
    print(
        "Record the answers in design.md's 'Still unverified' table and tick task 23b, amending "
        "Requirements 9.19 / 9.21 wherever reality disagreed."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
