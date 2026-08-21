# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 20 — the whole gate, once per importer, through the browser.

The other suites test parts: `test_validate_view.py` tests what an upload reports,
`test_apply_gate.py` tests what the gate refuses, and the three importer suites test what each one
decides. This file tests the claim the tool is *for*, end to end, on all three sheets:

1. upload a file with one bad row among good ones — it is refused, the issue is pinned to a file,
   sheet, line and column, and **NetBox received no write at all**, not even for the good rows;
2. a forged apply on that refused file is still refused, and still writes nothing;
3. fix the bad row, re-upload, apply — and the objects appear;
4. take a clean file, let NetBox change underneath it, apply — and it aborts having written nothing.

Nothing is stubbed at the HTTP layer here; the double is `FakeNetBox`, whose `posts` list is the
evidence. `assert netbox.posts == []` is the assertion this whole file exists for: an empty write
log is the only proof that a refusal was a refusal and not a rollback.

Requirements 10.1, 10.6, 10.7, 10.14, 16.1.
"""

from typing import Any

import pytest
from django.urls import reverse

from importer.models import ImportRun
from importer.models import Verdict as RunVerdict
from importer.tests.conftest import upload_of

pytestmark = pytest.mark.django_db

REGIONS = "dcim.regions"
SITES = "dcim.sites"
LOCATIONS = "dcim.locations"
RACKS = "dcim.racks"
DEVICES = "dcim.devices"
DEVICE_TYPES = "dcim.device-types"
MANUFACTURERS = "dcim.manufacturers"
ROLES = "dcim.device-roles"
INTERFACES = "dcim.interfaces"
REAR_PORTS = "dcim.rear-ports"
CABLES = "dcim.cables"


class Flow:
    """One operator, one page, driven the way the templates drive it."""

    def __init__(self, browser, netbox, page: str):
        self.browser = browser
        self.netbox = netbox
        self.page = page

    def validate(self, rows: list[dict[str, str]], filename: str = ""):
        upload = upload_of(self.page, rows, filename or f"{self.page}.csv")
        posted = self.browser.post(reverse(f"importer:{self.page}_validate"), {"sheet": upload})
        assert posted.status_code == 302, "validate redirects, so a refresh cannot re-upload"
        return self.browser.get(reverse(f"importer:{self.page}"))

    def apply(self, fingerprint: str = "", *, page=None):
        if not fingerprint:
            fingerprint = page.context["verdict"].fingerprint
        return self.browser.post(
            reverse(f"importer:{self.page}_apply"), {"fingerprint": fingerprint}
        )


# --- the three sheets, each with one deliberately broken row ---------------------------------


def seed_for_devices(netbox) -> dict[str, Any]:
    site = netbox.add(SITES, name="EXA1", slug="exa1")
    rack = netbox.add(
        RACKS, name="EXA1-DH1-R01", site={"id": site["id"], "name": "EXA1"}, u_height=45
    )
    manufacturer = netbox.add(MANUFACTURERS, name="Example Networks", slug="example-networks")
    netbox.add(
        DEVICE_TYPES,
        model="Example-Switch-32C",
        slug="example-switch-32c",
        manufacturer={"id": manufacturer["id"], "name": "Example Networks"},
        u_height=1,
    )
    netbox.add(ROLES, name="Unassigned", slug="unassigned")
    return {"site": site, "rack": rack}


def seed_for_cabling(netbox) -> dict[str, Any]:
    leaf = netbox.add(DEVICES, name="exa1-dh1-r01-leaf01")
    panel = netbox.add(DEVICES, name="exa1-dh1-r01-pp01")
    for index in range(1, 4):
        netbox.add(
            INTERFACES,
            name=f"swp{index}",
            device={"id": leaf["id"], "name": leaf["name"]},
            cable=None,
            type={"value": "100gbase-x-qsfp28"},
        )
        netbox.add(
            REAR_PORTS,
            name=f"MPO{index}",
            device={"id": panel["id"], "name": panel["name"]},
            cable=None,
        )
    return {"leaf": leaf, "panel": panel}


HIERARCHY = {
    "page": "hierarchy",
    "seed": lambda netbox: {},
    "good": [
        {
            "location": "Example Region",
            "datacenter": "EXA1",
            "datahall": "DH1",
            "rack": "EXA1-DH1-R01",
            "rack_u_height": "45",
        },
        {
            "location": "Example Region",
            "datacenter": "EXA1",
            "datahall": "DH1",
            "rack": "EXA1-DH1-R02",
            "rack_u_height": "45",
        },
    ],
    # The rack name names a datacenter this row does not (Requirement 6.3), which is the mistake a
    # copied-down spreadsheet row actually makes.
    "bad": {
        "location": "Example Region",
        "datacenter": "EXA1",
        "datahall": "DH1",
        "rack": "EXA9-DH1-R03",
        "rack_u_height": "45",
    },
    "fixed_rack": "EXA1-DH1-R03",
    "column": "rack",
    "endpoint": RACKS,
    "expected_objects": 6,  # region, site, datahall, three racks
    "drift": lambda netbox: netbox.add(REGIONS, name="Example Region", slug="example-region"),
}

DEVICES_SHEET = {
    "page": "devices",
    "seed": seed_for_devices,
    "good": [
        {
            "rack": "EXA1-DH1-R01",
            "rack_face": "front",
            "bottom_ru": "10",
            "ru_height": "1",
            "device_name": "exa1-dh1-r01-leaf01",
            "model": "Example-Switch-32C",
        },
        {
            "rack": "EXA1-DH1-R01",
            "rack_face": "front",
            "bottom_ru": "12",
            "ru_height": "1",
            "device_name": "exa1-dh1-r01-leaf02",
            "model": "Example-Switch-32C",
        },
    ],
    # U45 with a 1U device is fine; U46 is one unit past the top of a 45U rack (Requirement 8.4).
    "bad": {
        "rack": "EXA1-DH1-R01",
        "rack_face": "front",
        "bottom_ru": "46",
        "ru_height": "1",
        "device_name": "exa1-dh1-r01-leaf03",
        "model": "Example-Switch-32C",
    },
    "fix": {"bottom_ru": "14"},
    "column": "bottom_ru",
    "endpoint": DEVICES,
    "expected_objects": 3,
    # Somebody else mounts a device in U10 while the operator is reading the plan.
    "drift": lambda netbox: netbox.add(
        DEVICES,
        name="exa1-dh1-r01-someone-else",
        rack={"id": netbox.stored(RACKS)[0]["id"], "name": "EXA1-DH1-R01"},
        face={"value": "front"},
        position=10,
        device_type={"id": 1, "model": "Example-Switch-32C", "u_height": 1},
    ),
}

CABLING = {
    "page": "cabling",
    "seed": seed_for_cabling,
    "good": [
        {
            "a_end": "exa1-dh1-r01-leaf01",
            "a_end_kind": "device",
            "a_port": "swp1",
            "b_end": "exa1-dh1-r01-pp01",
            "b_end_kind": "panel",
            "b_port": "MPO1",
            "b_port_side": "rear",
            "cable_type": "smf-os2",
        },
        {
            "a_end": "exa1-dh1-r01-leaf01",
            "a_end_kind": "device",
            "a_port": "swp2",
            "b_end": "exa1-dh1-r01-pp01",
            "b_end_kind": "panel",
            "b_port": "MPO2",
            "b_port_side": "rear",
            "cable_type": "smf-os2",
        },
    ],
    # swp99 does not exist on this device (Requirement 9.4).
    "bad": {
        "a_end": "exa1-dh1-r01-leaf01",
        "a_end_kind": "device",
        "a_port": "swp99",
        "b_end": "exa1-dh1-r01-pp01",
        "b_end_kind": "panel",
        "b_port": "MPO3",
        "b_port_side": "rear",
        "cable_type": "smf-os2",
    },
    "fix": {"a_port": "swp3"},
    "column": "a_port",
    "endpoint": CABLES,
    "expected_objects": 3,
    # Somebody else patches swp1 while the operator is reading the plan.
    "drift": lambda netbox: occupy(netbox, INTERFACES, "swp1"),
}


def occupy(netbox, endpoint: str, port: str) -> None:
    """Attach a cable this tool knows nothing about to a port the plan is about to use."""
    cable = netbox.add(CABLES, label="someone-elses-cable")
    for record in netbox.stored(endpoint):
        if record.get("name") == port:
            record["cable"] = {"id": cable["id"]}


SHEETS = [HIERARCHY, DEVICES_SHEET, CABLING]
IDS = [sheet["page"] for sheet in SHEETS]


def fixed(sheet: dict[str, Any]) -> dict[str, str]:
    """The bad row, corrected the way the operator would correct it."""
    if "fix" in sheet:
        return {**sheet["bad"], **sheet["fix"]}
    return {**sheet["bad"], "rack": sheet["fixed_rack"]}


@pytest.fixture(params=SHEETS, ids=IDS)
def scenario(request, browser, netbox):
    sheet = request.param
    sheet["seed"](netbox)
    return sheet, Flow(browser, netbox, sheet["page"])


# --- 1 and 2: one bad row refuses the file, and nothing is written ---------------------------


class TestOneBadRowRefusesTheWholeFile:
    def test_the_file_is_blocked_and_netbox_receives_nothing(self, scenario, netbox):
        sheet, flow = scenario
        page = flow.validate([*sheet["good"], sheet["bad"]])
        verdict = page.context["verdict"]

        assert verdict.is_applyable() is False
        assert verdict.errors, "the bad row must produce at least one error"
        # Requirement 10.7: the good rows are refused along with the bad one.
        assert netbox.posts == []
        assert netbox.stored(sheet["endpoint"]) == []

    def test_the_issue_names_the_file_sheet_line_and_column(self, scenario):
        """Requirement 16.1: an engineer can go straight to the cell."""
        sheet, flow = scenario
        page = flow.validate([*sheet["good"], sheet["bad"]], filename="import-2026-08.csv")
        issue = next(
            issue
            for issue in page.context["verdict"].errors
            if issue.column == sheet["column"] and issue.line == 4
        )

        assert issue.file == "import-2026-08.csv"
        # Requirement 16.3: for a csv the worksheet reads as the file, so the format never varies.
        assert issue.sheet == "import-2026-08.csv"
        assert issue.line == 4, "header is line 1, so the third data row is line 4"
        assert issue.value and issue.expected and issue.message

    def test_the_page_offers_no_apply_control(self, scenario):
        sheet, flow = scenario
        page = flow.validate([*sheet["good"], sheet["bad"]])
        assert 'id="apply-form"' not in page.content.decode()

    def test_a_forged_apply_is_refused_and_still_writes_nothing(self, scenario, netbox):
        """Requirement 10.6: the missing button is a courtesy; this is the control."""
        sheet, flow = scenario
        page = flow.validate([*sheet["good"], sheet["bad"]])

        refused = flow.apply(page=page)
        assert refused.status_code == 409
        assert netbox.posts == []
        assert ImportRun.objects.get().verdict == RunVerdict.BLOCKED

    def test_the_refusal_is_recorded_with_its_issues(self, scenario):
        """Requirement 16.11: someone tried, the tool said no, and both facts survive."""
        sheet, flow = scenario
        flow.validate([*sheet["good"], sheet["bad"]])
        run = ImportRun.objects.get()
        assert run.error_count >= 1
        assert run.issues.filter(severity="error", column=sheet["column"]).exists()


# --- 3: fix the row, and it applies ---------------------------------------------------------


class TestFixingTheRowLetsItApply:
    def test_the_corrected_file_applies_and_the_objects_appear(self, scenario, netbox):
        sheet, flow = scenario
        blocked = flow.validate([*sheet["good"], sheet["bad"]])
        assert blocked.context["verdict"].is_applyable() is False

        page = flow.validate([*sheet["good"], fixed(sheet)])
        verdict = page.context["verdict"]
        assert verdict.is_applyable() is True

        applied = flow.apply(page=page)
        assert applied.status_code == 302

        run = ImportRun.objects.latest("pk")
        assert run.verdict == RunVerdict.APPLIED
        # `created_count` counts objects, not rows: three hierarchy rows create six objects.
        assert run.created_count == sheet["expected_objects"]
        assert verdict.object_count == sheet["expected_objects"]
        assert len(netbox.stored(sheet["endpoint"])) >= 3

    def test_re_uploading_the_applied_file_creates_nothing_the_second_time(self, scenario, netbox):
        """Requirement 11.7: idempotent end to end, not just in the planner."""
        sheet, flow = scenario
        page = flow.validate([*sheet["good"], fixed(sheet)])
        flow.apply(page=page)
        written = len(netbox.posts)

        again = flow.validate([*sheet["good"], fixed(sheet)])
        assert again.context["verdict"].is_applyable() is True
        assert again.context["verdict"].object_count == 0
        flow.apply(page=again)

        assert len(netbox.posts) == written, "a second apply of the same sheet wrote something"
        assert ImportRun.objects.latest("pk").created_count == 0


# --- 4: NetBox moves underneath a clean plan -------------------------------------------------


class TestNetBoxChangingBeforeApply:
    def test_the_apply_aborts_before_writing_anything(self, scenario, netbox):
        """Requirement 10.14: re-checked immediately before the first write, and abandoned."""
        sheet, flow = scenario
        page = flow.validate([*sheet["good"], fixed(sheet)])
        assert page.context["verdict"].is_applyable() is True

        sheet["drift"](netbox)

        applied = flow.apply(page=page)
        assert applied.status_code == 302

        run = ImportRun.objects.latest("pk")
        assert run.verdict == RunVerdict.STALE
        assert run.created_count == 0
        assert netbox.posts == []
        assert "Validate the file again" in run.aborted_reason

    def test_the_report_says_plainly_that_nothing_was_written(self, scenario, netbox):
        sheet, flow = scenario
        page = flow.validate([*sheet["good"], fixed(sheet)])
        sheet["drift"](netbox)
        flow.apply(page=page)

        run = ImportRun.objects.latest("pk")
        body = flow.browser.get(reverse("importer:run_detail", args=[run.pk])).content.decode()
        assert "Nothing was written" in run.aborted_reason
        assert "Nothing was written" in body
