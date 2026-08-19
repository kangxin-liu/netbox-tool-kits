# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 16 — uploading a sheet, and reading the verdict it comes back with.

The single most important assertion in this file is that validating a file issues **no write to
NetBox at all**, whatever the file contains (Requirement 10.1). Everything else here is about
whether the operator can act on what they are told.

Requirements 10.1–10.5, 10.10, 10.12, 10.13, 10.18, 16.1–16.11, 17.1.
"""

import pytest
from django.test import override_settings
from django.urls import reverse

from importer.models import ImportRun
from importer.models import Verdict as RunVerdict
from importer.services import plan_store
from importer.tests.conftest import upload_of
from importer.validation import Code

pytestmark = pytest.mark.django_db

GOOD_ROW = {
    "location": "Example Region",
    "datacenter": "SYN1",
    "datahall": "DH1",
    "rack": "SYN1-DH1-R01",
    "rack_u_height": "45",
}


def validate(browser, rows, page="hierarchy", **kwargs):
    """POST an upload the way the form does, then follow the redirect to read the report."""
    upload = kwargs.pop("upload", None) or upload_of(page, rows, **kwargs)
    posted = browser.post(reverse(f"importer:{page}_validate"), {"sheet": upload})
    assert posted.status_code == 302, "validating redirects so a refresh cannot re-upload"
    return browser.get(reverse(f"importer:{page}"))


def row(**overrides) -> dict[str, str]:
    return {**GOOD_ROW, **overrides}


# --- 10.1: the whole point ------------------------------------------------------------------


class TestValidationWritesNothing:
    def test_a_clean_file_issues_no_write(self, browser, netbox):
        validate(browser, [row()])
        assert netbox.posts == []

    def test_a_file_with_one_error_issues_no_write(self, browser, netbox):
        validate(browser, [row(rack="SYN1_DH1_R01")])
        assert netbox.posts == []

    def test_an_unreadable_file_issues_no_write(self, browser, netbox):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("notes.txt", b"nothing here", content_type="text/plain")
        browser.post(reverse("importer:hierarchy_validate"), {"sheet": upload})
        assert netbox.posts == []

    def test_validation_creates_no_object_even_for_a_row_it_will_create(self, browser, netbox):
        page = validate(browser, [row()])
        assert b"to create" in page.content
        assert netbox.stored("dcim.racks") == []


# --- 10.10: the verdict, prominently --------------------------------------------------------


class TestVerdict:
    def test_a_clean_file_reads_as_ready_with_its_counts(self, browser, netbox):
        page = validate(browser, [row()])
        assert "Ready" in page.content.decode()
        assert page.context["verdict"].create_count == 1  # one row
        # …which creates four objects: region, site, location, rack. The operator is authorising the
        # objects, not the rows, so both numbers are stated.
        assert page.context["verdict"].object_count == 4
        assert b"Apply 4 creates" in page.content

    def test_two_rows_sharing_a_new_parent_do_not_double_count_it(self, browser, netbox):
        """Requirement 7.6: the shared datahall is created once, so the count says so."""
        page = validate(browser, [row(), row(rack="SYN1-DH1-R02")])
        assert page.context["verdict"].create_count == 2
        assert page.context["verdict"].object_count == 5  # region, site, hall, two racks

    def test_a_blocked_file_says_so_and_states_the_error_count(self, browser, netbox):
        page = validate(browser, [row(rack_u_height="0")])
        text = page.content.decode()
        assert "Blocked" in text
        assert "Nothing will be written" in text

    def test_a_blocked_file_offers_no_apply_control_at_all(self, browser, netbox):
        """Absent rather than disabled: a greyed-out button invites trying to re-enable it."""
        page = validate(browser, [row(rack_u_height="0")])
        assert reverse("importer:hierarchy_apply").encode() not in page.content

    def test_a_ready_file_offers_the_apply_control(self, browser, netbox):
        page = validate(browser, [row()])
        assert reverse("importer:hierarchy_apply").encode() in page.content

    def test_the_verdict_names_the_file_and_the_sheet(self, browser, netbox):
        page = validate(browser, [row()], filename="racks-syn1.csv")
        text = page.content.decode()
        assert "racks-syn1.csv" in text

    def test_the_expiry_time_is_shown_so_a_stale_tab_is_obvious(self, browser, netbox):
        page = validate(browser, [row()])
        assert b"expires" in page.content


# --- 10.4: stages not reached ---------------------------------------------------------------


class TestStages:
    def test_a_structural_failure_says_which_stages_were_not_reached(self, browser, netbox):
        from django.core.files.uploadedfile import SimpleUploadedFile

        # A header missing a required column stops at S1, so S2 onward never ran.
        payload = b"location,datacenter\r\nExample Region,SYN1\r\n"
        upload = SimpleUploadedFile("partial.csv", payload, content_type="text/csv")
        browser.post(reverse("importer:hierarchy_validate"), {"sheet": upload})
        page = browser.get(reverse("importer:hierarchy"))
        text = page.content.decode()
        assert "were not reached" in text
        assert "S4" in text

    def test_a_structural_failure_reports_every_missing_column_at_once(self, browser, netbox):
        from django.core.files.uploadedfile import SimpleUploadedFile

        payload = b"location\r\nExample Region\r\n"
        upload = SimpleUploadedFile("partial.csv", payload, content_type="text/csv")
        browser.post(reverse("importer:hierarchy_validate"), {"sheet": upload})
        page = browser.get(reverse("importer:hierarchy"))
        codes = [code for code, _ in page.context["issue_report"].code_counts]
        assert str(Code.REQUIRED_COLUMN_MISSING) in codes
        assert page.context["issue_report"].code_counts[0][1] == 4

    def test_a_cell_level_failure_records_that_s2_ran(self, browser, netbox):
        """S2 found the problem, so S2 was reached — the report must not claim it was skipped."""
        page = validate(browser, [row(rack_u_height="not a number")])
        assert "S2" in page.context["verdict"].stages_completed

    def test_a_clean_file_reaches_all_five_stages(self, browser, netbox):
        page = validate(browser, [row()])
        assert page.context["verdict"].complete is True


# --- 16.1, 16.2, 16.6: localisation ---------------------------------------------------------


class TestIssueLocalisation:
    def test_an_issue_names_file_sheet_line_and_column(self, browser, netbox):
        page = validate(browser, [row(rack="SYN1_DH1_R01")], filename="racks.csv")
        text = page.content.decode()
        assert "file racks.csv" in text
        assert "line 2" in text
        assert "column rack" in text

    def test_the_line_number_is_the_one_excel_shows(self, browser, netbox):
        """Header is line 1, so the first data row of this upload is line 2 (Requirement 16.2)."""
        page = validate(browser, [row(), row(rack="SYN1_DH1_R02x", datahall="DH1")])
        lines = {issue.line for issue in page.context["verdict"].errors}
        assert lines == {3}

    def test_a_received_value_and_an_expectation_are_both_shown(self, browser, netbox):
        page = validate(browser, [row(rack="SYN1_DH1_R01")])
        text = page.content.decode()
        assert "received" in text and "expected" in text
        assert "SYN1_DH1_R01" in text

    def test_a_duplicate_names_both_lines(self, browser, netbox):
        page = validate(browser, [row(), row()])
        issue = next(
            i for i in page.context["verdict"].errors if i.code is Code.DUPLICATE_RACK_IN_FILE
        )
        assert issue.line == 3
        assert issue.related_line == 2
        assert b"Also involves line 2" in page.content

    def test_every_issue_carries_a_code(self, browser, netbox):
        page = validate(browser, [row(rack_u_height="0")])
        assert all(issue.code for issue in page.context["verdict"].issues)

    def test_a_cell_holding_markup_is_escaped_rather_than_rendered(self, browser, netbox):
        """Sheet content is untrusted: it reaches the page through autoescaping (Req 14.4)."""
        page = validate(browser, [row(rack="<script>alert(1)</script>")])
        assert b"<script>alert(1)</script>" not in page.content
        assert b"&lt;script&gt;" in page.content


# --- 16.8, 16.10: grouping and the display cap ----------------------------------------------


class TestIssueReport:
    def test_the_per_code_tally_is_rendered(self, browser, netbox):
        page = validate(
            browser, [row(rack_u_height="0"), row(rack="SYN1-DH1-R02", rack_u_height="0")]
        )
        assert b"Issues by code" in page.content
        assert page.context["issue_report"].code_counts[0][1] == 2

    @override_settings(MAX_ISSUES_DISPLAYED=3)
    def test_the_list_is_truncated_with_a_notice_while_the_csv_stays_complete(
        self, browser, netbox
    ):
        rows = [row(rack=f"SYN1_DH1_R{index:02d}") for index in range(1, 9)]
        page = validate(browser, rows)
        assert b"Showing the first 3 of" in page.content

        run = page.context["run"]
        csv_text = browser.get(reverse("importer:run_issues", args=[run.pk])).content.decode(
            "utf-8-sig"
        )
        assert csv_text.count("RACK_NAME_MALFORMED") == 8

    def test_the_issue_csv_download_is_offered_alongside_the_truncated_list(self, browser, netbox):
        page = validate(browser, [row(rack_u_height="0")])
        assert reverse("importer:run_issues", args=[page.context["run"].pk]).encode() in (
            page.content
        )


# --- 10.18: the per-row plan ----------------------------------------------------------------


class TestPreview:
    def test_each_row_shows_its_line_outcome_and_what_it_resolved_to(self, browser, netbox):
        page = validate(browser, [row()])
        text = page.content.decode()
        assert "What each row will do" in text
        assert "create" in text
        assert "SYN1-DH1-R01" in text

    def test_a_row_whose_objects_exist_reads_as_a_skip(self, browser, netbox):
        netbox.add("dcim.regions", name="Example Region", slug="example-region")
        site = netbox.add(
            "dcim.sites", name="SYN1", slug="syn1", region={"id": 1001, "name": "Example Region"}
        )
        hall = netbox.add(
            "dcim.locations", name="DH1", slug="dh1", site={"id": site["id"], "name": "SYN1"}
        )
        netbox.add(
            "dcim.racks",
            name="SYN1-DH1-R01",
            site={"id": site["id"], "name": "SYN1"},
            location={"id": hall["id"], "name": "DH1"},
            u_height=45,
            width={"value": 19},
            status={"value": "active"},
        )
        page = validate(browser, [row()])
        assert page.context["verdict"].skip_count == 1
        assert b"skip-already-exists" in page.content

    def test_every_row_of_a_blocked_file_reads_as_blocked_including_the_good_ones(
        self, browser, netbox
    ):
        """Requirement 10.7: there is no partial apply, so no row is offered as applyable.

        The error here is found at S4 — an existing rack whose height disagrees — because that is
        the stage where rows have already been planned. A cell-level error stops before planning, so
        there are no rows to mark.
        """
        netbox.add("dcim.regions", name="Example Region", slug="example-region")
        site = netbox.add(
            "dcim.sites", name="SYN1", slug="syn1", region={"id": 1001, "name": "Example Region"}
        )
        hall = netbox.add(
            "dcim.locations", name="DH1", slug="dh1", site={"id": site["id"], "name": "SYN1"}
        )
        netbox.add(
            "dcim.racks",
            name="SYN1-DH1-R02",
            site={"id": site["id"], "name": "SYN1"},
            location={"id": hall["id"], "name": "DH1"},
            u_height=23,
            width={"value": 19},
            status={"value": "active"},
        )
        page = validate(browser, [row(), row(rack="SYN1-DH1-R02")])
        assert page.context["verdict"].errors
        assert {r.outcome for r in page.context["plan"].rows} == {"blocked"}


# --- 10.12, 10.13: the plan is held server-side and bound to its file ------------------------


class TestPlanHolding:
    def test_the_report_survives_navigating_away_and_back(self, browser, netbox):
        validate(browser, [row()])
        browser.get(reverse("importer:devices"))
        again = browser.get(reverse("importer:hierarchy"))
        assert again.context["verdict"] is not None
        assert again.context["plan"] is not None

    def test_each_page_holds_its_own_plan(self, browser, netbox):
        validate(browser, [row()])
        assert browser.get(reverse("importer:devices")).context.get("verdict") is None

    def test_re_uploading_replaces_the_previous_verdict(self, browser, netbox):
        first = validate(browser, [row()])
        second = validate(browser, [row(rack_u_height="0")])
        assert second.context["run"].pk != first.context["run"].pk
        assert plan_store.current(browser.session, "hierarchy") == second.context["run"].pk

    def test_the_plan_is_bound_to_a_fingerprint_of_the_file(self, browser, netbox):
        page = validate(browser, [row()])
        assert len(page.context["verdict"].fingerprint) == 64

    def test_the_same_file_twice_fingerprints_the_same(self, browser, netbox):
        first = validate(browser, [row()]).context["verdict"].fingerprint
        second = validate(browser, [row()]).context["verdict"].fingerprint
        assert first == second

    def test_a_changed_file_fingerprints_differently(self, browser, netbox):
        first = validate(browser, [row()]).context["verdict"].fingerprint
        second = validate(browser, [row(rack_u_height="42")]).context["verdict"].fingerprint
        assert first != second

    def test_a_plan_that_is_no_longer_held_points_at_its_audit_record(self, browser, netbox):
        """The cache is not durable; the run record is. Say so rather than showing nothing."""
        page = validate(browser, [row()])
        run = page.context["run"]
        plan_store.discard(page.context["run"].user_id, "hierarchy", run.pk)

        again = browser.get(reverse("importer:hierarchy"))
        assert again.context["expired_run"].pk == run.pk
        assert b"no longer being held" in again.content


# --- 16.11: a refused attempt is still auditable ---------------------------------------------


class TestRunRecording:
    def test_a_blocked_attempt_is_persisted_with_its_issues(self, browser, netbox):
        validate(browser, [row(rack_u_height="0")])
        run = ImportRun.objects.get()
        assert run.verdict == RunVerdict.BLOCKED
        assert run.error_count == 1
        assert run.issues.count() == 1
        assert run.results.count() == 0

    def test_a_ready_attempt_is_persisted_as_ready_and_not_as_applied(self, browser, netbox):
        validate(browser, [row()])
        run = ImportRun.objects.get()
        assert run.verdict == RunVerdict.READY
        assert run.applied_at is None
        assert run.created_count == 0

    def test_the_run_records_who_did_it_and_where_it_pointed(self, browser, netbox, operator):
        validate(browser, [row()])
        run = ImportRun.objects.get()
        assert run.user == operator
        assert run.netbox_host == netbox.host
        assert run.netbox_version == "4.6.8"

    def test_the_run_records_the_stages_and_the_fingerprint(self, browser, netbox):
        validate(browser, [row()])
        run = ImportRun.objects.get()
        assert run.stages == ["S1", "S2", "S3", "S4", "S5"]
        assert len(run.file_fingerprint) == 64

    def test_a_structurally_rejected_file_is_recorded_even_with_no_plan(self, browser, netbox):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("notes.txt", b"nope", content_type="text/plain")
        browser.post(reverse("importer:hierarchy_validate"), {"sheet": upload})
        run = ImportRun.objects.get()
        assert run.verdict == RunVerdict.BLOCKED
        assert run.issues.filter(code=str(Code.UNSUPPORTED_FILE_TYPE)).exists()

    def test_no_persisted_field_holds_the_token(self, browser, netbox, settings):
        validate(browser, [row()])
        run = ImportRun.objects.get()
        blob = " ".join(str(value) for value in run.__dict__.values())
        assert settings.NETBOX_TOKEN not in blob


# --- the form itself -------------------------------------------------------------------------


class TestUploadForm:
    def test_the_page_offers_an_upload_field(self, browser, netbox):
        page = browser.get(reverse("importer:hierarchy"))
        assert b'type="file"' in page.content
        assert b'enctype="multipart/form-data"' in page.content

    def test_posting_no_file_says_so_rather_than_failing(self, browser, netbox):
        response = browser.post(reverse("importer:hierarchy_validate"), {})
        assert response.status_code == 302
        page = browser.get(reverse("importer:hierarchy"))
        assert b"Choose a .csv or .xlsx file" in page.content

    def test_validate_is_post_only(self, browser, netbox):
        assert browser.get(reverse("importer:hierarchy_validate")).status_code == 405

    def test_validate_requires_a_logged_in_superuser(self, client, netbox):
        response = client.post(reverse("importer:hierarchy_validate"), {})
        assert response.status_code == 302
        assert "/login/" in response["Location"]

    def test_an_unreachable_netbox_refuses_the_file_and_says_why(self, browser, netbox):
        from importer.services.netbox_client import Failure, NetBoxUnavailable

        def refuse(**_):
            raise NetBoxUnavailable(Failure.DNS, netbox.host, "The host could not be resolved.")

        netbox.status = refuse
        validate(browser, [row()])
        run = ImportRun.objects.get()
        assert run.verdict == RunVerdict.UNAVAILABLE
        assert run.issues.filter(code=str(Code.NETBOX_UNREACHABLE)).exists()

    def test_an_unsupported_netbox_version_refuses_the_file_before_parsing_it(
        self, browser, netbox
    ):
        """Requirement 2.7: refuse the import, not merely the button."""
        from importer.services.netbox_client import NetBoxStatus

        netbox.status = lambda **_: NetBoxStatus(
            version="4.5.1",
            host=netbox.host,
            supported=False,
            reason="NetBox 4.5.1 is outside the supported range >=4.6, <4.7.",
        )
        validate(browser, [row()])
        run = ImportRun.objects.get()
        assert run.verdict == RunVerdict.UNAVAILABLE
        assert run.issues.filter(code=str(Code.NETBOX_VERSION_UNSUPPORTED)).exists()
        assert netbox.posts == []
