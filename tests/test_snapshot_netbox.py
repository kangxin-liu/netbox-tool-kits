# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.

from __future__ import annotations

import unittest
from typing import Any

from scripts.snapshot_netbox import Restorer, SnapshotError


class FakeNetBox:
    def __init__(
        self,
        writable: dict[str, set[str]],
        records: dict[str, list[dict[str, Any]]] | None = None,
    ):
        self._writable = writable
        self._records = records or {}

    def writable_fields(self, endpoint: str) -> set[str]:
        return self._writable[endpoint]

    def all(self, endpoint: str) -> list[dict[str, Any]]:
        return list(self._records.get(endpoint, []))


def snapshot(*entries: tuple[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {"endpoints": [{"path": endpoint, "records": records} for endpoint, records in entries]}


class RestorerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manufacturer = {
            "id": 1,
            "url": "https://netbox.example.test/api/dcim/manufacturers/1/",
            "name": "Example Manufacturer",
            "slug": "example-manufacturer",
        }
        self.device_type = {
            "id": 2,
            "url": "https://netbox.example.test/api/dcim/device-types/2/",
            "manufacturer": self.manufacturer,
            "model": "Example Model",
            "slug": "example-model",
            "subdevice_role": {"value": "", "label": "Parent/child status"},
        }
        self.writable = {
            "dcim/manufacturers": {"name", "slug"},
            "dcim/device-types": {
                "manufacturer",
                "model",
                "slug",
                "subdevice_role",
            },
        }

    def test_payload_remaps_relationships_and_choices(self) -> None:
        client = FakeNetBox(self.writable)
        restorer = Restorer(
            client,
            snapshot(
                ("dcim/manufacturers", [self.manufacturer]),
                ("dcim/device-types", [self.device_type]),
            ),
            merge=False,
        )
        restorer.id_map[("dcim/manufacturers", 1)] = 101

        payload = restorer.payload("dcim/device-types", self.device_type)

        self.assertEqual(payload["manufacturer"], 101)
        self.assertEqual(payload["subdevice_role"], "")
        self.assertNotIn("id", payload)
        self.assertNotIn("url", payload)

    def test_validation_rejects_uncaptured_relationship(self) -> None:
        client = FakeNetBox(self.writable)
        restorer = Restorer(
            client,
            snapshot(("dcim/device-types", [self.device_type])),
            merge=False,
        )

        with self.assertRaisesRegex(SnapshotError, "Snapshot is incomplete"):
            restorer.validate_relationships()

    def test_nonempty_target_requires_explicit_merge(self) -> None:
        client = FakeNetBox(
            self.writable,
            records={"dcim/manufacturers": [self.manufacturer]},
        )
        restorer = Restorer(
            client,
            snapshot(("dcim/manufacturers", [self.manufacturer])),
            merge=False,
        )

        with self.assertRaisesRegex(SnapshotError, "Target is not empty"):
            restorer.preflight()

    def test_merge_accepts_nonempty_target(self) -> None:
        client = FakeNetBox(
            self.writable,
            records={"dcim/manufacturers": [self.manufacturer]},
        )
        restorer = Restorer(
            client,
            snapshot(("dcim/manufacturers", [self.manufacturer])),
            merge=True,
        )

        restorer.preflight()


if __name__ == "__main__":
    unittest.main()
