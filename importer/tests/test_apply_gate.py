# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 17 — the gate, tested the way it will be attacked.

Every test here answers the same question in a different way: can anything reach NetBox that was not
a clean, current, acknowledged, unapplied plan? A forged POST, a stale tab, an expired verdict, a
second submission, and a NetBox that moved under us are each tried, and each must end with **zero
writes**.

Requirements 1.5, 3.3, 10.6–10.17, 14.3, 15.4.
"""

from datetime import timedelta

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from importer.models import ImportRun
from importer.models import Verdict as RunVerdict
from importer.services import plan_store
from importer.services.netbox_client import Failure, NetBoxUnavailable
from importer.tests.conftest import upload_of

pytestmark = pytest.mark.django_db

RACKS = "dcim.racks"

GOOD_ROW = {
    "location": "Example Region",
    "datacenter": "SYN1",
    "datahall": "DH1",
    "rack": "SYN1-DH1-R01",
    "rack_u_height": "45",
}


def row(**overrides) -> dict[str, str]:
    return {**GOOD_ROW, **overrides}


def validate(browser, rows, page="hierarchy"):
    browser.post(reverse(f"importer:{page}_validate"), {"sheet": upload_of(page, rows)})
    return browser.get(reverse(f"importer:{page}"))


def held_of(browser, page="hierarchy"):
    run_id = plan_store.current(browser.session, page)
    return plan_store.fetch(ImportRun.objects.get(pk=run_id).user_id, page, run_id)


def restore(browser, held, page="hierarchy"):
    plan_store.store(ImportRun.objects.get(pk=held.run_id).user_id, held)


def apply_now(browser, fingerprint=None, page="hierarchy"):
    held = held_of(browser, page)
    data = {"fingerprint": fingerprint if fingerprint is not None else held.verdict.fingerprint}
    return browser.post(reverse(f"importer:{page}_apply"), data)


# --- 10.6, 10.7: a blocked plan cannot be applied by any request shape ------------------------


class TestBlockedPlansAreRefused:
    def test_a_forged_post_on_a_blocked_plan_returns_409_and_writes_nothing(self, browser, netbox):
        page = validate(browser, [row(rack_u_height="0")])
        fingerprint = page.context["verdict"].fingerprint

        response = browser.post(reverse("importer:hierarchy_apply"), {"fingerprint": fingerprint})
        assert response.status_code == 409
        assert netbox.posts == []

    def test_the_refusal_explains_itself_rather_than_just_failing(self, browser, netbox):
        validate(browser, [row(rack_u_height="0")])
        response = apply_now(browser)
        text = response.content.decode()
        assert "error(s) block this file" in text
        assert "no partial apply" in text

    def test_partial_application_is_not_reachable_by_naming_a_good_line(self, browser, netbox):
        """Requirement 10.7: there is no request shape that applies the valid rows only."""
        validate(browser, [row(), row(rack="SYN1-DH1-R02", rack_u_height="0")])
        response = browser.post(
            reverse("importer:hierarchy_apply"),
            {"fingerprint": held_of(browser).verdict.fingerprint, "line": "2", "rows": "2"},
        )
        assert response.status_code == 409
        assert netbox.posts == []

    def test_a_structurally_rejected_file_has_no_plan_to_apply(self, browser, netbox):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("notes.txt", b"nope", content_type="text/plain")
        browser.post(reverse("importer:hierarchy_validate"), {"sheet": upload})
        response = browser.post(reverse("importer:hierarchy_apply"), {"fingerprint": ""})
        assert response.status_code == 409
        assert netbox.posts == []

    def test_applying_with_no_plan_held_at_all_is_refused(self, browser, netbox):
        response = browser.post(reverse("importer:hierarchy_apply"), {"fingerprint": "whatever"})
        assert response.status_code == 409
        assert b"No validated plan" in response.content
        assert netbox.posts == []


# --- 10.9: warnings are acknowledged, never assumed -------------------------------------------


class TestWarningAcknowledgement:
    @pytest.fixture
    def warned(self, browser, netbox):
        """A file whose only problem is a descriptive difference — a warning, not a blocker (D8)."""
        netbox.add("dcim.regions", name="Example Region", slug="example-region")
        site = netbox.add(
            "dcim.sites", name="SYN1", slug="syn1", region={"id": 1001, "name": "Example Region"}
        )
        netbox.add(
            "dcim.locations", name="DH1", slug="dh1", site={"id": site["id"], "name": "SYN1"}
        )
        netbox.add(
            RACKS,
            name="SYN1-DH1-R01",
            site={"id": site["id"], "name": "SYN1"},
            location={"id": 1003, "name": "DH1"},
            u_height=45,
            width={"value": 19},
            status={"value": "planned"},
        )
        return validate(browser, [row(rack_status="active")])

    def test_the_file_warns_rather_than_blocks(self, warned):
        assert warned.context["verdict"].errors == []
        assert warned.context["verdict"].warnings

    def test_unacknowledged_warnings_are_not_applyable(self, warned, browser, netbox):
        response = apply_now(browser)
        assert response.status_code == 409
        assert b"must be acknowledged" in response.content
        assert netbox.posts == []

    def test_the_page_offers_no_apply_control_until_they_are_acknowledged(self, warned):
        assert reverse("importer:hierarchy_apply").encode() not in warned.content
        assert reverse("importer:hierarchy_ack").encode() in warned.content

    def test_acknowledging_makes_it_applyable_and_is_recorded(self, warned, browser, netbox):
        browser.post(reverse("importer:hierarchy_ack"))
        page = browser.get(reverse("importer:hierarchy"))
        assert page.context["verdict"].needs_acknowledgement is False
        assert ImportRun.objects.get().warnings_acknowledged is True

        assert apply_now(browser).status_code == 302

    def test_acknowledgement_is_post_only(self, warned, browser):
        assert browser.get(reverse("importer:hierarchy_ack")).status_code == 405


# --- 10.13, 10.15: the plan is bound to its file and does not keep -----------------------------


class TestStaleTabs:
    def test_a_mismatched_fingerprint_is_refused(self, browser, netbox):
        validate(browser, [row()])
        response = apply_now(browser, fingerprint="0" * 64)
        assert response.status_code == 409
        assert b"showing a different file" in response.content
        assert netbox.posts == []

    def test_an_expired_verdict_is_refused_and_says_when_it_expired(self, browser, netbox):
        validate(browser, [row()])
        held = held_of(browser)
        held.verdict.validated_at = timezone.now() - timedelta(minutes=31)
        restore(browser, held)

        response = apply_now(browser)
        assert response.status_code == 409
        assert b"expired" in response.content
        assert netbox.posts == []

    @override_settings(PLAN_EXPIRY_MINUTES=1)
    def test_the_expiry_window_is_configurable(self, browser, netbox):
        validate(browser, [row()])
        held = held_of(browser)
        held.verdict.validated_at = timezone.now() - timedelta(minutes=2)
        restore(browser, held)
        assert apply_now(browser).status_code == 409

    def test_re_uploading_after_a_refusal_replaces_the_plan(self, browser, netbox):
        validate(browser, [row(rack_u_height="0")])
        assert apply_now(browser).status_code == 409

        validate(browser, [row()])
        assert apply_now(browser).status_code == 302
        assert netbox.stored(RACKS)


# --- 10.14: NetBox moving between validation and apply ----------------------------------------


class TestStaleNetBox:
    def test_an_object_appearing_after_validation_aborts_before_the_first_write(
        self, browser, netbox
    ):
        validate(browser, [row()])

        # Someone else creates the region between validation and apply.
        netbox.add("dcim.regions", name="Example Region", slug="example-region")

        response = apply_now(browser)
        assert response.status_code == 302
        run = ImportRun.objects.get()
        assert run.verdict == RunVerdict.STALE
        assert run.created_count == 0
        assert netbox.posts == []
        assert "NetBox changed after this file was validated" in run.aborted_reason

    def test_the_abort_tells_the_operator_to_validate_again(self, browser, netbox):
        validate(browser, [row()])
        netbox.add("dcim.regions", name="Example Region", slug="example-region")
        apply_now(browser)
        assert "Validate the file again" in ImportRun.objects.get().aborted_reason

    def test_netbox_disappearing_before_the_first_write_aborts_cleanly(self, browser, netbox):
        validate(browser, [row()])
        netbox.unavailable_on_read["dcim.regions"] = NetBoxUnavailable(
            Failure.CONNECTION, netbox.host, "Could not open a connection."
        )

        response = apply_now(browser)
        assert response.status_code == 302
        run = ImportRun.objects.get()
        assert run.verdict == RunVerdict.STALE
        assert netbox.posts == []
        assert "nothing was written" in run.aborted_reason.lower()

    def test_an_unsupported_version_at_apply_time_refuses_the_write(self, browser, netbox):
        from importer.services.netbox_client import NetBoxStatus

        validate(browser, [row()])
        netbox.status = lambda **_: NetBoxStatus(
            version="4.7.0", host=netbox.host, supported=False, reason="outside the supported range"
        )
        response = apply_now(browser)
        assert response.status_code == 409
        assert netbox.posts == []


# --- 10.16: applying twice ---------------------------------------------------------------------


class TestSecondApply:
    def test_the_second_apply_is_refused_and_points_at_the_report(self, browser, netbox):
        validate(browser, [row()])
        first = apply_now(browser)
        assert first.status_code == 302

        second = apply_now(browser)
        assert second.status_code == 409
        assert b"already been applied" in second.content
        assert b"report" in second.content

    def test_the_second_apply_writes_nothing_further(self, browser, netbox):
        validate(browser, [row()])
        apply_now(browser)
        posts_after_first = len(netbox.posts)
        apply_now(browser)
        assert len(netbox.posts) == posts_after_first

    def test_re_validating_the_same_file_after_applying_finds_everything_present(
        self, browser, netbox
    ):
        """Requirement 11.7: the honest way to re-run a sheet is to validate it again."""
        validate(browser, [row()])
        apply_now(browser)

        page = validate(browser, [row()])
        assert page.context["verdict"].errors == []
        assert page.context["verdict"].skip_count == 1
        assert page.context["verdict"].object_count == 0


# --- the happy path, and what it records -------------------------------------------------------


class TestApplying:
    def test_a_clean_plan_creates_its_objects_and_redirects_to_the_report(self, browser, netbox):
        validate(browser, [row()])
        response = apply_now(browser)
        run = ImportRun.objects.get()

        assert response["Location"] == reverse("importer:run_detail", args=[run.pk])
        assert [record["name"] for record in netbox.stored(RACKS)] == ["SYN1-DH1-R01"]
        assert run.verdict == RunVerdict.APPLIED
        assert run.created_count == 4
        assert run.applied_at is not None

    def test_each_created_object_is_recorded_with_its_id_and_a_link(self, browser, netbox):
        validate(browser, [row()])
        apply_now(browser)
        rack = ImportRun.objects.get().results.get(object_type="rack")
        assert rack.object_id is not None
        assert rack.object_name == "SYN1-DH1-R01"
        assert rack.url.startswith(f"https://{netbox.host}/")
        assert rack.sequence > 0

    def test_the_apply_is_audited_with_the_actor(self, browser, netbox, caplog):
        validate(browser, [row()])
        with caplog.at_level("INFO", logger="importer.audit"):
            apply_now(browser)
        applied = [r.getMessage() for r in caplog.records if "apply finished" in r.getMessage()]
        assert applied and "user=operator" in applied[0]

    def test_apply_is_post_only(self, browser, netbox):
        validate(browser, [row()])
        assert browser.get(reverse("importer:hierarchy_apply")).status_code == 405

    def test_apply_requires_a_csrf_token(self, client, operator, netbox):
        """Requirement 14.3, checked with CSRF enforcement on rather than the test default off."""
        client.force_login(operator)
        client.post(
            reverse("importer:hierarchy_validate"), {"sheet": upload_of("hierarchy", [row()])}
        )

        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        strict.force_login(operator)
        response = strict.post(reverse("importer:hierarchy_apply"), {"fingerprint": "x"})
        assert response.status_code == 403
        assert netbox.posts == []

    def test_apply_requires_a_superuser(self, client, django_user_model, netbox):
        ordinary = django_user_model.objects.create_user(
            username="ordinary", password="synthetic-password"
        )
        client.force_login(ordinary)
        assert client.post(reverse("importer:hierarchy_apply"), {}).status_code == 403
        assert netbox.posts == []

    def test_the_page_shows_a_busy_state_so_the_operator_does_not_double_submit(
        self, browser, netbox
    ):
        """Requirement 15.4. The server refuses a duplicate anyway; this is so it never comes up."""
        page = validate(browser, [row()])
        assert b"apply-busy" in page.content
        assert b"js/apply.js" in page.content


# --- 1.5: logging out clears what was held -----------------------------------------------------


class TestLogout:
    def test_logging_out_drops_the_held_plan(self, browser, netbox, operator):
        validate(browser, [row()])
        run_id = plan_store.current(browser.session, "hierarchy")

        browser.post(reverse("logout"))
        assert plan_store.fetch(operator.pk, "hierarchy", run_id) is None

    def test_the_run_record_survives_the_logout(self, browser, netbox):
        validate(browser, [row()])
        browser.post(reverse("logout"))
        assert ImportRun.objects.count() == 1

    def test_logging_back_in_shows_no_pending_plan(self, browser, netbox, operator):
        validate(browser, [row()])
        browser.post(reverse("logout"))
        browser.force_login(operator)
        page = browser.get(reverse("importer:hierarchy"))
        assert page.context.get("verdict") is None
