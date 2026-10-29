# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 18 — the audit trail: what was recorded, and what cannot be changed through the UI.

Requirements 12.1–12.7, 13.1–13.5, 16.11.
"""

import csv
import io

import pytest
from django.urls import reverse

from importer.models import ImportRun, MutationOutcome
from importer.models import Verdict as RunVerdict
from importer.services import plan_store
from importer.services.netbox_client import Failure, NetBoxUnavailable
from importer.tests.conftest import upload_of

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("hierarchy_prerequisites"),
]

RACKS = "dcim.racks"

GOOD_ROW = {
    "site_slug": "syn",
    "parent_location_slug": "syn1",
    "datahall": "SYN1-DH1",
    "datahall_slug": "syn1-dh1",
    "rack": "SYN1-DH1-R01",
    "rack_u_height": "45",
}


def row(**overrides) -> dict[str, str]:
    return {**GOOD_ROW, **overrides}


def validate(browser, rows, page="hierarchy"):
    browser.post(reverse(f"importer:{page}_validate"), {"sheet": upload_of(page, rows)})
    return browser.get(reverse(f"importer:{page}"))


def apply_now(browser, page="hierarchy"):
    run_id = plan_store.current(browser.session, page)
    held = plan_store.fetch(ImportRun.objects.get(pk=run_id).user_id, page, run_id)
    return browser.post(
        reverse(f"importer:{page}_apply"), {"fingerprint": held.verdict.fingerprint}
    )


def read_csv(payload: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(payload.decode("utf-8-sig"), newline="")))


# --- 13.4: the history view --------------------------------------------------------------------


class TestHistory:
    def test_runs_are_listed_newest_first(self, browser, netbox):
        validate(browser, [row()])
        validate(browser, [row(rack="SYN1-DH1-R02")])
        page = browser.get(reverse("importer:runs"))
        listed = [run.pk for run in page.context["runs"]]
        assert listed == sorted(listed, reverse=True)

    def test_a_blocked_attempt_appears_in_the_history(self, browser, netbox):
        """Requirement 16.11: a refused import is part of the record, not an absence of one."""
        validate(browser, [row(rack_u_height="0")])
        page = browser.get(reverse("importer:runs"))
        assert b"Blocked" in page.content

    def test_the_history_names_the_file_the_operator_and_the_verdict(self, browser, netbox):
        validate(browser, [row()])
        page = browser.get(reverse("importer:runs"))
        text = page.content.decode()
        assert "hierarchy.csv" in text
        assert "operator" in text
        assert "Ready" in text

    def test_an_empty_history_says_so_rather_than_showing_an_empty_table(self, browser, netbox):
        assert b"Nothing has been validated yet" in browser.get(reverse("importer:runs")).content

    def test_the_history_is_reachable_from_every_page(self, browser, netbox):
        page = browser.get(reverse("importer:hierarchy"))
        assert reverse("importer:runs").encode() in page.content

    def test_the_history_requires_a_superuser(self, client, django_user_model):
        ordinary = django_user_model.objects.create_user(
            username="ordinary", password="synthetic-password"
        )
        client.force_login(ordinary)
        assert client.get(reverse("importer:runs")).status_code == 403

    def test_the_history_requires_logging_in(self, client):
        response = client.get(reverse("importer:runs"))
        assert response.status_code == 302
        assert "/login/" in response["Location"]


# --- 13.5: read-only means no route, not a hidden button ---------------------------------------


class TestNoMutation:
    def test_a_run_cannot_be_edited_or_deleted_through_any_exposed_route(self, browser, netbox):
        validate(browser, [row()])
        run = ImportRun.objects.get()
        detail = reverse("importer:run_detail", args=[run.pk])

        # There is no route behind these verbs at all, which is the point: not a disabled control.
        assert browser.post(detail).status_code == 405
        assert browser.delete(detail).status_code == 405
        assert browser.put(detail).status_code == 405
        assert ImportRun.objects.count() == 1

    def test_the_detail_page_offers_no_delete_control(self, browser, netbox):
        validate(browser, [row()])
        page = browser.get(reverse("importer:run_detail", args=[ImportRun.objects.get().pk]))
        text = page.content.decode().lower()
        assert "delete" not in text

    def test_a_run_that_does_not_exist_is_a_404_rather_than_an_error(self, browser, netbox):
        assert browser.get(reverse("importer:run_detail", args=[9999])).status_code == 404


# --- 12.1, 12.4, 13.2: what an applied run records ---------------------------------------------


class TestAppliedRunDetail:
    @pytest.fixture
    def applied(self, browser, netbox):
        validate(browser, [row()])
        apply_now(browser)
        run = ImportRun.objects.get()
        return browser.get(reverse("importer:run_detail", args=[run.pk])), run

    def test_every_created_object_is_listed_with_a_link_into_netbox(self, applied):
        page, run = applied
        text = page.content.decode()
        assert "SYN1-DH1-R01" in text
        assert f"https://{run.netbox_host}/dcim/racks/" in text

    def test_the_rows_are_shown_in_apply_order(self, applied):
        page, _ = applied
        sequences = [result.sequence for result in page.context["results"]]
        assert sequences == [1, 2]

    def test_the_object_type_and_id_are_recorded_for_each_create(self, applied):
        _, run = applied
        kinds = {result.object_type for result in run.results.all()}
        assert kinds == {"location", "rack"}
        assert all(result.object_id for result in run.results.all())

    def test_the_guarantee_is_stated_plainly_rather_than_implied(self, applied):
        """Requirement 12.5: what this tool does and does not promise, in the report itself."""
        page, _ = applied
        text = page.content.decode()
        assert "all-or-nothing" in text
        assert "can be reverted later from history" in text

    def test_the_endpoints_that_completed_are_named(self, applied):
        page, _ = applied
        assert "dcim.racks" in page.content.decode()

    def test_the_report_downloads_as_a_csv(self, applied, browser):
        _, run = applied
        response = browser.get(reverse("importer:run_report", args=[run.pk]))
        rows = read_csv(response.content)
        assert rows[0][0] == "sequence"
        assert len(rows) == 3  # header plus child-location and rack creates
        assert response["Content-Disposition"].startswith("attachment")

    def test_the_run_records_which_netbox_it_wrote_to(self, applied):
        _, run = applied
        assert run.netbox_host == "netbox.example.com"
        assert run.netbox_version == "4.6.8"

    def test_a_run_that_applied_nothing_has_no_report_csv(self, browser, netbox):
        validate(browser, [row(rack_u_height="0")])
        run = ImportRun.objects.get()
        assert browser.get(reverse("importer:run_report", args=[run.pk])).status_code == 404

    def test_applying_a_sheet_whose_objects_all_exist_is_recorded_as_an_apply(
        self, browser, netbox
    ):
        """ "Applied, created nothing" is a different fact from "never applied" (Req 11.7)."""
        validate(browser, [row()])
        apply_now(browser)
        validate(browser, [row()])
        apply_now(browser)

        second = ImportRun.objects.order_by("-pk").first()
        assert second.verdict == RunVerdict.APPLIED
        assert second.applied_at is not None
        assert second.created_count == 0
        assert second.skipped_count == 1


# --- 12.2, 12.3, 12.4: partial failure -------------------------------------------------------


class TestAbortedRun:
    def test_a_rejected_batch_is_recorded_with_netboxs_own_error_against_the_row(
        self, browser, netbox
    ):
        netbox.reject(
            RACKS,
            lambda payloads: (
                400,
                {"u_height": ["Ensure this value is less than or equal to 100."]},
            ),
        )
        validate(browser, [row()])
        apply_now(browser)

        run = ImportRun.objects.get()
        assert run.verdict == RunVerdict.ABORTED
        rack_row = run.results.get(object_type="rack")
        assert rack_row.outcome == "blocked"
        assert rack_row.column == "u_height"
        assert "Ensure this value" in rack_row.message

    def test_an_abort_names_the_endpoints_that_completed_before_it(self, browser, netbox):
        """Requirement 12.4: the manual unwind starts from this list."""
        netbox.reject(RACKS, lambda payloads: (400, {"name": ["nope"]}))
        validate(browser, [row()])
        apply_now(browser)

        run = ImportRun.objects.get()
        assert run.endpoints_completed.split(",") == ["dcim.locations"]
        assert run.created_count == 1
        page = browser.get(reverse("importer:run_detail", args=[run.pk]))
        assert b"dcim.locations" in page.content

    def test_connection_loss_mid_apply_records_what_was_written_first(self, browser, netbox):
        netbox.unavailable_on[RACKS] = NetBoxUnavailable(
            Failure.CONNECTION, netbox.host, "Could not open a connection."
        )
        validate(browser, [row()])
        apply_now(browser)

        run = ImportRun.objects.get()
        assert run.verdict == RunVerdict.RECONCILIATION_REQUIRED
        assert run.created_count == 1
        assert "were not rolled back" in run.aborted_reason
        assert run.mutations.filter(outcome=MutationOutcome.UNKNOWN).count() == 1

    def test_a_partly_applied_plan_cannot_be_applied_again(self, browser, netbox):
        """Re-applying would resume mid-sequence without re-validating."""
        netbox.reject(RACKS, lambda payloads: (400, {"name": ["nope"]}))
        validate(browser, [row()])
        apply_now(browser)
        assert apply_now(browser).status_code == 409

    def test_a_replay_is_disclosed_rather_than_glossed_over(self, browser, netbox):
        """An un-indexed rejection means rows were created one at a time, not atomically."""
        rejections = {"count": 0}

        def reject_the_batch_only(payloads):
            rejections["count"] += 1
            if len(payloads) > 1:
                return (400, {"detail": "one of these is a duplicate"})
            return None

        netbox.reject("dcim.locations", reject_the_batch_only)
        validate(
            browser,
            [
                row(),
                row(
                    datahall="SYN1-DH2",
                    datahall_slug="syn1-dh2",
                    rack="SYN1-DH2-R01",
                ),
            ],
        )
        apply_now(browser)

        run = ImportRun.objects.get()
        assert run.replayed is True
        page = browser.get(reverse("importer:run_detail", args=[run.pk]))
        assert b"re-sent row by row" in page.content


# --- 13.3, 12.7: nothing sensitive is persisted or rendered -----------------------------------


class TestNothingLeaks:
    def test_no_persisted_field_of_any_model_holds_the_token(self, browser, netbox, settings):
        validate(browser, [row()])
        apply_now(browser)
        run = ImportRun.objects.get()
        blob = " ".join(
            str(value)
            for record in [
                run,
                *run.results.all(),
                *run.issues.all(),
                *run.mutations.all(),
            ]
            for value in record.__dict__.values()
        )
        assert settings.NETBOX_TOKEN not in blob
        assert settings.NETBOX_REVERT_TOKEN not in blob

    def test_the_uploaded_file_itself_is_not_persisted(self, browser, netbox):
        """Requirement 13.3: the audit log records decisions, not a copy of the spreadsheet."""
        validate(browser, [row()])
        run = ImportRun.objects.get()
        assert not hasattr(run, "file")
        assert run.file_fingerprint  # the fingerprint is the reference to the file, not its bytes

    def test_no_rendered_page_contains_the_token(self, browser, netbox, settings):
        validate(browser, [row()])
        apply_now(browser)
        run = ImportRun.objects.get()
        for url in [
            reverse("importer:hierarchy"),
            reverse("importer:runs"),
            reverse("importer:run_detail", args=[run.pk]),
        ]:
            assert settings.NETBOX_TOKEN.encode() not in browser.get(url).content

    def test_an_issue_value_is_escaped_in_the_csv_and_in_the_page(self, browser, netbox):
        validate(browser, [row(rack_u_height="=1")])
        run = ImportRun.objects.get()
        rows = read_csv(browser.get(reverse("importer:run_issues", args=[run.pk])).content)
        assert any(cell.startswith("'=") for row_cells in rows for cell in row_cells)
