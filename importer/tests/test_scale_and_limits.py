# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 19 — the bounds an apply runs inside, and what happens at them.

Apply runs inline in the request (decision D5), so the only honest way to handle a file too big to
finish is to refuse it before starting: a run that times out halfway is exactly the half-populated
NetBox this tool exists to prevent. Requirement 15.3 therefore makes the row count a validation
error, and Requirement 15.4 makes a running apply visibly busy.

The tests here are about three claims:

* the bounds are **stated before** an upload rather than discovered by tripping them,
* a file over the limit is **blocked with nothing written**, and stays unappliable if the operator
  forges the apply anyway,
* 2,000 rows validate without one NetBox request per row (Requirements 15.1, 15.2) — the property
  that makes the limit a *policy* rather than a symptom of a slow implementation.
"""

import io

import pytest
from django.test import override_settings
from django.urls import reverse

from importer.importers.hierarchy import HierarchyImporter
from importer.services.lookups import Lookups
from importer.services.sheets import parse_upload
from importer.tests.conftest import upload_of
from importer.validation import Code

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("hierarchy_prerequisites"),
]

BASE_ROW = {
    "site_slug": "syn",
    "parent_location_slug": "syn1",
    "datahall": "SYN1-DH1",
    "datahall_slug": "syn1-dh1",
    "rack_u_height": "45",
}


def rows(count: int) -> list[dict[str, str]]:
    """`count` distinct racks in one datahall — the shape a real bulk sheet has."""
    return [{**BASE_ROW, "rack": f"SYN1-DH1-R{index:04d}"} for index in range(1, count + 1)]


def validate(browser, sheet_rows, page="hierarchy"):
    posted = browser.post(
        reverse(f"importer:{page}_validate"), {"sheet": upload_of(page, sheet_rows)}
    )
    assert posted.status_code == 302
    return browser.get(reverse(f"importer:{page}"))


def parse(sheet_rows, filename: str):
    """The same csv the form would send, parsed without going through a view."""
    payload = upload_of("hierarchy", sheet_rows).read()
    return parse_upload(
        "hierarchy",
        filename=filename,
        stream=io.BytesIO(payload),
        size=len(payload),
        content_type="text/csv",
    )


# --- 15.3: stated up front ------------------------------------------------------------------


class TestTheBoundsAreVisible:
    @override_settings(MAX_APPLY_ROWS=1500, MAX_UPLOAD_MB=7)
    def test_the_page_states_the_row_and_size_limits_before_anything_is_uploaded(
        self, browser, netbox
    ):
        page = browser.get(reverse("importer:hierarchy"))
        body = page.content.decode()
        assert "1500 data rows" in body
        assert "7 MB" in body
        # And says what to do about it, which is the half operators actually need.
        assert "split it" in body

    def test_each_page_states_them(self, browser, netbox):
        for page in ("hierarchy", "devices", "cabling"):
            body = browser.get(reverse(f"importer:{page}")).content.decode()
            assert "data rows" in body


# --- 15.3: over the limit, nothing written --------------------------------------------------


class TestOverTheLimit:
    @override_settings(MAX_APPLY_ROWS=5)
    def test_a_file_over_the_limit_is_blocked_with_the_limit_stated(self, browser, netbox):
        page = validate(browser, rows(6))
        verdict = page.context["verdict"]
        error = next(i for i in verdict.errors if i.code is Code.ROW_COUNT_OVER_LIMIT)

        assert verdict.is_applyable() is False
        assert error.value == "6"
        assert "at most 5 rows" in error.expected
        # Requirement 16.5: the message says what to do, not just what is wrong.
        assert "Split it" in error.message or "split it" in error.message.lower()
        assert netbox.posts == []

    @override_settings(MAX_APPLY_ROWS=5)
    def test_the_limit_is_on_rows_not_on_the_objects_they_create(self, browser, netbox):
        """Five rows create eight objects here, and five rows is what the bound is about.

        The bound exists because apply runs in one request, and the request's cost tracks the rows
        parsed and planned. Counting objects instead would refuse a small sheet that happens to
        create a deep hierarchy.
        """
        page = validate(browser, rows(5))
        assert page.context["verdict"].is_applyable() is True
        assert page.context["verdict"].object_count > 5

    @override_settings(MAX_APPLY_ROWS=5)
    def test_the_bound_survives_a_forged_apply(self, browser, netbox):
        """The visible refusal is a courtesy; this is the control (Requirement 10.6)."""
        page = validate(browser, rows(6))
        fingerprint = page.context["verdict"].fingerprint

        refused = browser.post(reverse("importer:hierarchy_apply"), {"fingerprint": fingerprint})
        assert refused.status_code == 409
        assert netbox.posts == []

    @override_settings(MAX_APPLY_ROWS=5)
    def test_a_file_exactly_at_the_limit_is_allowed(self, browser, netbox):
        """An off-by-one here would refuse the file the operator was told they could send."""
        page = validate(browser, rows(5))
        assert page.context["verdict"].is_applyable() is True


# --- 15.1, 15.2: 2,000 rows, without a request per row --------------------------------------


class TestScale:
    def test_two_thousand_rows_validate_with_a_handful_of_requests(self, netbox):
        """Requirements 15.1, 15.2, and the reason the 2,000-row default is not arbitrary.

        Deliberately not through the view: this measures the importer's request behaviour, and a
        view test would add a page probe's requests to the count and obscure it.
        """
        sheet = parse(rows(2000), "big.csv")
        assert len(sheet.rows) == 2000

        netbox.request_count = 0
        plan = HierarchyImporter().validate(sheet, Lookups(client=netbox))

        assert plan.verdict.is_applyable() is True
        assert plan.verdict.row_count == 2000
        # Prefetch per endpoint, plus the choice lists. The exact number matters less than that it
        # does not scale with the row count: one request per row would be 2,000.
        assert netbox.request_count < 40, (
            f"{netbox.request_count} requests for 2,000 rows looks like a per-row lookup"
        )

    def test_the_plan_is_one_action_per_object_not_per_row(self, netbox):
        """Requirement 11.6: many racks under one parent create one child location."""
        plan = HierarchyImporter().validate(parse(rows(50), "fifty.csv"), Lookups(client=netbox))
        assert plan.verdict.object_count == 51


# --- 15.4: the busy state --------------------------------------------------------------------


class TestBusyState:
    def test_an_appliable_plan_renders_the_busy_affordance_and_its_script(self, browser, netbox):
        page = validate(browser, rows(1))
        body = page.content.decode()
        assert 'id="apply-form"' in body
        assert "data-busy-label" in body
        assert "js/apply.js" in body

    def test_a_blocked_plan_renders_no_apply_form_to_be_busy_about(self, browser, netbox):
        page = validate(browser, [{**BASE_ROW, "rack": "R01", "rack_u_height": "0"}])
        assert 'id="apply-form"' not in page.content.decode()
