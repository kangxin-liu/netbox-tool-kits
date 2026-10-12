# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""D14 mutation history, drift-gated preflight, and compensating revert."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from importer.breakout_tags import TAGS_ENDPOINT
from importer.models import (
    ImportMutation,
    ImportRun,
    MutationAction,
    MutationOutcome,
    RevertItemOutcome,
    RevertStatus,
)
from importer.services import revert
from importer.tests.test_run_history import apply_now, row, validate

pytestmark = pytest.mark.django_db

RACKS = "dcim.racks"
DEVICES = "dcim.devices"
INTERFACES = "dcim.interfaces"


def applied_run(operator, netbox) -> ImportRun:
    now = timezone.now()
    return ImportRun.objects.create(
        importer="hierarchy",
        user=operator,
        filename="synthetic.csv",
        netbox_host=netbox.host,
        netbox_version=netbox.version,
        validated_at=now,
        applied_at=now,
        finished_at=now,
        verdict="applied",
    )


def create_mutation(run, netbox, *, endpoint=RACKS, sequence=1, name="SYN-R01"):
    record = netbox.add(endpoint, name=name, status="active")
    current = netbox.get_one(endpoint, record["id"])
    return ImportMutation.objects.create(
        run=run,
        sequence=sequence,
        action=MutationAction.CREATE,
        endpoint=endpoint,
        source_lines=[sequence + 2],
        natural_key=[name],
        object_type="rack",
        object_id=record["id"],
        object_name=name,
        url=netbox.object_url(endpoint, record["id"]),
        after_values={"name": name, "status": "active"},
        post_last_updated=current["last_updated"],
        outcome=MutationOutcome.APPLIED,
        applied_at=timezone.now(),
    )


class TestMutationJournalIntegration:
    def test_apply_records_one_mutation_per_created_object(self, browser, netbox):
        validate(browser, [row()])
        apply_now(browser)

        run = ImportRun.objects.get()
        mutations = list(run.mutations.all())
        assert len(mutations) == 4
        assert [item.sequence for item in mutations] == [1, 2, 3, 4]
        assert all(item.outcome == MutationOutcome.APPLIED for item in mutations)
        assert all(item.object_id and item.post_last_updated for item in mutations)

    def test_history_shows_summary_and_mutation_csv(self, browser, netbox):
        validate(browser, [row()])
        apply_now(browser)
        run = ImportRun.objects.get()

        page = browser.get(reverse("importer:run_detail", args=[run.pk]))
        text = page.content.decode()
        assert "Applied changes" in text
        assert "still applied" in text
        response = browser.get(reverse("importer:run_mutations", args=[run.pk]))
        assert response.status_code == 200
        assert b"before_values" in response.content


class TestRevertService:
    def test_full_create_revert_deletes_by_id(self, operator, netbox):
        run = applied_run(operator, netbox)
        mutation = create_mutation(run, netbox)

        attempt = revert.preflight(run=run, user=operator, client=netbox)
        assert attempt.status == RevertStatus.READY
        revert.execute(attempt=attempt, user=operator, client=netbox)

        attempt.refresh_from_db()
        assert attempt.status == RevertStatus.REVERTED
        assert netbox.get_one(RACKS, mutation.object_id) is None
        assert netbox.deletes == [(RACKS, mutation.object_id)]
        assert not revert.eligibility(run).allowed

    def test_unrelated_edit_blocks_every_delete(self, operator, netbox):
        run = applied_run(operator, netbox)
        mutation = create_mutation(run, netbox)
        netbox.touch(RACKS, mutation.object_id)

        attempt = revert.preflight(run=run, user=operator, client=netbox)

        assert attempt.status == RevertStatus.PREFLIGHT_BLOCKED
        assert "drift" in attempt.failure_reason
        assert netbox.deletes == []
        assert attempt.items.get().outcome == RevertItemOutcome.DRIFTED

    def test_interface_tag_update_revert_restores_the_exact_prior_set(self, operator, netbox):
        run = applied_run(operator, netbox)
        old_tag = netbox.add(TAGS_ENDPOINT, name="uplink", slug="uplink")
        breakout_tag = netbox.add(
            TAGS_ENDPOINT,
            name="port-breakout-4x",
            slug="port-breakout-4x",
        )
        interface = netbox.add(
            INTERFACES,
            name="swp1",
            tags=[
                {"id": old_tag["id"], "name": old_tag["name"], "slug": old_tag["slug"]},
                {
                    "id": breakout_tag["id"],
                    "name": breakout_tag["name"],
                    "slug": breakout_tag["slug"],
                },
            ],
        )
        current = netbox.get_one(INTERFACES, interface["id"])
        mutation = ImportMutation.objects.create(
            run=run,
            sequence=1,
            action=MutationAction.UPDATE,
            endpoint=INTERFACES,
            source_lines=[2],
            natural_key=["leaf01", "swp1", "tags"],
            object_type="interface tags",
            object_id=interface["id"],
            object_name="leaf01 swp1 breakout tag",
            before_values={"tags": [old_tag["id"]]},
            after_values={"tags": [old_tag["id"], breakout_tag["id"]]},
            post_last_updated=current["last_updated"],
            outcome=MutationOutcome.APPLIED,
            applied_at=timezone.now(),
        )

        attempt = revert.preflight(run=run, user=operator, client=netbox)
        assert attempt.status == RevertStatus.READY
        revert.execute(attempt=attempt, user=operator, client=netbox)

        restored = netbox.get_one(INTERFACES, interface["id"])
        assert [tag["id"] for tag in restored["tags"]] == [old_tag["id"]]
        assert netbox.patches[-1].payloads == [{"id": mutation.object_id, "tags": [old_tag["id"]]}]

    def test_host_mismatch_blocks_before_reading_or_writing(self, operator, netbox):
        run = applied_run(operator, netbox)
        create_mutation(run, netbox)
        netbox.host = "other.example.com"

        attempt = revert.preflight(run=run, user=operator, client=netbox)

        assert attempt.status == RevertStatus.PREFLIGHT_BLOCKED
        assert "does not match" in attempt.failure_reason
        assert netbox.deletes == []

    def test_reverse_order_and_partial_failure_are_durable(self, operator, netbox):
        run = applied_run(operator, netbox)
        parent = create_mutation(run, netbox, sequence=1, name="parent")
        child = create_mutation(run, netbox, sequence=2, name="child")
        netbox.reject(
            f"DELETE:{RACKS}",
            lambda payloads: (409, {"detail": ["still referenced"]})
            if payloads[0]["id"] == parent.object_id
            else None,
        )

        attempt = revert.preflight(run=run, user=operator, client=netbox)
        revert.execute(attempt=attempt, user=operator, client=netbox)

        attempt.refresh_from_db()
        assert attempt.status == RevertStatus.PARTIALLY_REVERTED
        assert netbox.deletes == [(RACKS, child.object_id), (RACKS, parent.object_id)]
        assert netbox.get_one(RACKS, child.object_id) is None
        assert netbox.get_one(RACKS, parent.object_id) is not None
        outcomes = list(attempt.items.values_list("outcome", flat=True))
        assert outcomes == [RevertItemOutcome.REVERTED, RevertItemOutcome.FAILED]

        netbox.rejections.pop(f"DELETE:{RACKS}")
        retry = revert.preflight(run=run, user=operator, client=netbox)
        assert list(retry.items.values_list("mutation_id", flat=True)) == [parent.pk]
        revert.execute(attempt=retry, user=operator, client=netbox)
        assert retry.status == RevertStatus.REVERTED

    def test_drift_between_preflight_and_confirm_causes_zero_writes(self, operator, netbox):
        run = applied_run(operator, netbox)
        mutation = create_mutation(run, netbox)
        attempt = revert.preflight(run=run, user=operator, client=netbox)
        netbox.touch(RACKS, mutation.object_id)

        revert.execute(attempt=attempt, user=operator, client=netbox)

        assert attempt.status == RevertStatus.ABORTED
        assert netbox.deletes == []

    def test_expired_plan_causes_zero_writes(self, operator, netbox):
        run = applied_run(operator, netbox)
        create_mutation(run, netbox)
        attempt = revert.preflight(run=run, user=operator, client=netbox)
        attempt.expires_at = timezone.now() - timedelta(seconds=1)
        attempt.save(update_fields=["expires_at"])

        revert.execute(attempt=attempt, user=operator, client=netbox)

        assert attempt.status == RevertStatus.ABORTED
        assert netbox.deletes == []

    def test_update_revert_restores_only_recorded_fields(self, operator, netbox):
        run = applied_run(operator, netbox)
        record = netbox.add(DEVICES, name="leaf01", primary_ip4=None, status="active")
        record["primary_ip4"] = 42
        netbox.touch(DEVICES, record["id"])
        current = netbox.get_one(DEVICES, record["id"])
        mutation = ImportMutation.objects.create(
            run=run,
            sequence=1,
            action=MutationAction.UPDATE,
            endpoint=DEVICES,
            source_lines=[3],
            natural_key=["leaf01"],
            object_type="device primary IP",
            object_id=record["id"],
            object_name="leaf01",
            before_values={"primary_ip4": None},
            after_values={"primary_ip4": 42},
            post_last_updated=current["last_updated"],
            outcome=MutationOutcome.APPLIED,
            applied_at=timezone.now(),
        )

        attempt = revert.preflight(run=run, user=operator, client=netbox)
        revert.execute(attempt=attempt, user=operator, client=netbox)

        restored = netbox.get_one(DEVICES, mutation.object_id)
        assert restored["primary_ip4"] is None
        assert restored["status"] == "active"
        assert netbox.patches[-1].payloads == [{"id": mutation.object_id, "primary_ip4": None}]


class TestRevertViews:
    def test_preflight_then_confirm_reverts_an_applied_run(self, browser, netbox):
        validate(browser, [row()])
        apply_now(browser)
        run = ImportRun.objects.get()

        preflight = browser.post(reverse("importer:revert_validate", args=[run.pk]))
        assert preflight.status_code == 200
        attempt = run.revert_attempts.get()
        assert b"Confirm revert" in preflight.content

        response = browser.post(reverse("importer:revert_confirm", args=[run.pk, attempt.pk]))
        assert response.status_code == 302
        attempt.refresh_from_db()
        assert attempt.status == RevertStatus.REVERTED
        assert len(netbox.deletes) == 4
        browser.post(reverse("importer:revert_confirm", args=[run.pk, attempt.pk]))
        assert len(netbox.deletes) == 4

    def test_revert_routes_are_post_only(self, browser, netbox):
        validate(browser, [row()])
        apply_now(browser)
        run = ImportRun.objects.get()
        assert browser.get(reverse("importer:revert_validate", args=[run.pk])).status_code == 405

    def test_legacy_applied_run_is_not_guessed_at(self, browser, operator, netbox):
        run = applied_run(operator, netbox)
        page = browser.get(reverse("importer:run_detail", args=[run.pk]))
        assert b"legacy record" in page.content
        assert reverse("importer:revert_validate", args=[run.pk]).encode() not in page.content
