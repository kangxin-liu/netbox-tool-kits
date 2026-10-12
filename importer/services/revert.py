# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Preflight and execute guarded inverse operations from the mutation journal (D14)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from secrets import compare_digest
from typing import Any

from django.conf import settings
from django.utils import timezone
from django.utils.crypto import salted_hmac

from importer.models import (
    ImportMutation,
    ImportRun,
    MutationAction,
    MutationOutcome,
    RevertAttempt,
    RevertItemOutcome,
    RevertItemResult,
    RevertStatus,
)
from importer.services.mutations import canonical, projected
from importer.services.netbox_client import NetBoxUnavailable, NetBoxWriteRejected

logger = logging.getLogger("importer.audit")


@dataclass(frozen=True)
class RevertEligibility:
    mutations: tuple[ImportMutation, ...]
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return bool(self.mutations) and not self.reason


@dataclass(frozen=True)
class MutationSummary:
    created: int = 0
    changed: int = 0
    failed: int = 0
    unknown: int = 0
    reverted: int = 0
    still_applied: int = 0


def mutation_summary(run: ImportRun, *, reverted_ids: set[int] | None = None) -> MutationSummary:
    mutations = list(run.mutations.all())
    if reverted_ids is None:
        reverted_ids = set(
            RevertItemResult.objects.filter(
                mutation__run=run,
                outcome=RevertItemOutcome.REVERTED,
            ).values_list("mutation_id", flat=True)
        )
    applied = [item for item in mutations if item.outcome == MutationOutcome.APPLIED]
    return MutationSummary(
        created=len([item for item in applied if item.action == MutationAction.CREATE]),
        changed=len([item for item in applied if item.action == MutationAction.UPDATE]),
        failed=len([item for item in mutations if item.outcome == MutationOutcome.FAILED]),
        unknown=len(
            [
                item
                for item in mutations
                if item.outcome in (MutationOutcome.INTENT, MutationOutcome.UNKNOWN)
            ]
        ),
        reverted=len([item for item in applied if item.pk in reverted_ids]),
        still_applied=len([item for item in applied if item.pk not in reverted_ids]),
    )


def eligibility(run: ImportRun) -> RevertEligibility:
    unknown = run.mutations.filter(
        outcome__in=(MutationOutcome.INTENT, MutationOutcome.UNKNOWN)
    ).exists()
    if unknown:
        return RevertEligibility((), "Revert unavailable until uncertain writes are reconciled.")

    all_mutations = run.mutations.all()
    if not all_mutations.exists():
        reason = (
            "Revert unavailable — legacy record."
            if run.was_applied
            else "This run made no recorded changes."
        )
        return RevertEligibility((), reason)

    reverted_ids = RevertItemResult.objects.filter(
        mutation__run=run,
        outcome=RevertItemOutcome.REVERTED,
    ).values_list("mutation_id", flat=True)
    remaining = tuple(
        all_mutations.filter(outcome=MutationOutcome.APPLIED)
        .exclude(pk__in=reverted_ids)
        .order_by("-sequence", "-id")
    )
    if not remaining:
        return RevertEligibility((), "All recorded changes from this run have been reverted.")
    return RevertEligibility(remaining)


def preflight(*, run: ImportRun, user: Any, client: Any) -> RevertAttempt:
    """Read every target and persist either a blocked report or a confirmable plan."""
    attempt = RevertAttempt.objects.create(
        original_run=run,
        user=user,
        netbox_host=getattr(client, "host", ""),
        status=RevertStatus.PREFLIGHT_BLOCKED,
    )
    if getattr(client, "host", "") != run.netbox_host:
        return _block(
            attempt,
            f"Configured NetBox host {getattr(client, 'host', '')!r} does not match "
            f"the run host {run.netbox_host!r}.",
        )

    eligible = eligibility(run)
    if not eligible.allowed:
        return _block(attempt, eligible.reason)

    fingerprint_rows: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for reverse_sequence, mutation in enumerate(eligible.mutations, start=1):
        try:
            current = (
                client.get_one(mutation.endpoint, mutation.object_id)
                if mutation.object_id is not None
                else None
            )
            expected_values, expected_last_updated = _expected_post_state(mutation)
            conflict = _drift_reason(
                mutation,
                current,
                expected_values=expected_values,
                expected_last_updated=expected_last_updated,
            )
        except NetBoxUnavailable as unavailable:
            current = None
            conflict = unavailable.operator_message
        outcome = RevertItemOutcome.DRIFTED if conflict else RevertItemOutcome.PLANNED
        RevertItemResult.objects.create(
            attempt=attempt,
            mutation=mutation,
            reverse_sequence=reverse_sequence,
            outcome=outcome,
            message=conflict,
        )
        if conflict:
            conflicts.append(conflict)
        else:
            fingerprint_rows.append(_fingerprint_row(mutation, current or {}))

    if conflicts:
        return _block(
            attempt,
            f"Revert blocked by {len(conflicts)} drift conflict(s).",
            preserve_items=True,
        )

    attempt.expires_at = timezone.now() + timedelta(minutes=settings.REVERT_PLAN_EXPIRY_MINUTES)
    attempt.plan_fingerprint = _fingerprint(
        run=run,
        user=user,
        host=client.host,
        expires_at=attempt.expires_at,
        rows=fingerprint_rows,
    )
    attempt.status = RevertStatus.READY
    attempt.failure_reason = ""
    attempt.save(
        update_fields=[
            "plan_fingerprint",
            "status",
            "expires_at",
            "failure_reason",
        ]
    )
    logger.info(
        "revert preflight user=%s run=%s attempt=%s items=%d status=ready",
        getattr(user, "username", "-"),
        run.pk,
        attempt.pk,
        len(fingerprint_rows),
    )
    return attempt


def execute(*, attempt: RevertAttempt, user: Any, client: Any) -> RevertAttempt:
    """Repeat the drift gate, then compensate one operation at a time."""
    if attempt.user_id != user.pk:
        return _abort(attempt, "This revert plan belongs to another user.")
    if attempt.status != RevertStatus.READY:
        return attempt
    if attempt.expires_at is None or timezone.now() >= attempt.expires_at:
        return _abort(attempt, "The revert plan expired. Run preflight again.")
    if client.host != attempt.original_run.netbox_host:
        return _abort(attempt, "The configured NetBox host changed. Run preflight again.")

    items = list(attempt.items.select_related("mutation").order_by("reverse_sequence"))
    fingerprint_rows: list[dict[str, Any]] = []
    conflicts: dict[int, str] = {}
    for item in items:
        mutation = item.mutation
        try:
            current = (
                client.get_one(mutation.endpoint, mutation.object_id)
                if mutation.object_id is not None
                else None
            )
            expected_values, expected_last_updated = _expected_post_state(mutation)
            reason = _drift_reason(
                mutation,
                current,
                expected_values=expected_values,
                expected_last_updated=expected_last_updated,
            )
        except NetBoxUnavailable as unavailable:
            current = None
            reason = unavailable.operator_message
        if reason:
            conflicts[item.pk] = reason
        else:
            fingerprint_rows.append(_fingerprint_row(mutation, current or {}))

    fingerprint = _fingerprint(
        run=attempt.original_run,
        user=user,
        host=client.host,
        expires_at=attempt.expires_at,
        rows=fingerprint_rows,
    )
    if conflicts or not compare_digest(fingerprint, attempt.plan_fingerprint):
        for item in items:
            item.outcome = (
                RevertItemOutcome.DRIFTED
                if item.pk in conflicts
                else RevertItemOutcome.NOT_ATTEMPTED
            )
            item.message = conflicts.get(item.pk, "Not attempted because another item drifted.")
            item.save(update_fields=["outcome", "message"])
        return _abort(
            attempt,
            "NetBox changed after preflight. Nothing was reverted; run preflight again.",
        )

    successes = 0
    for index, item in enumerate(items):
        mutation = item.mutation
        try:
            if mutation.action == MutationAction.CREATE:
                client.delete(mutation.endpoint, int(mutation.object_id))
            else:
                client.restore(
                    mutation.endpoint,
                    int(mutation.object_id),
                    dict(mutation.before_values),
                )
        except (NetBoxUnavailable, NetBoxWriteRejected) as error:
            try:
                reconciled = _reconcile_inverse(mutation, client)
            except NetBoxUnavailable:
                reconciled = None
            if reconciled is True:
                _mark_reverted(item)
                successes += 1
                continue
            item.outcome = (
                RevertItemOutcome.UNKNOWN if reconciled is None else RevertItemOutcome.FAILED
            )
            item.message = str(error)[:500]
            item.save(update_fields=["outcome", "message"])
            for remaining in items[index + 1 :]:
                remaining.outcome = RevertItemOutcome.NOT_ATTEMPTED
                remaining.message = "Not attempted because the previous inverse write failed."
                remaining.save(update_fields=["outcome", "message"])
            _checkpoint_items(items[index + 1 :], client)
            attempt.status = RevertStatus.PARTIALLY_REVERTED if successes else RevertStatus.ABORTED
            attempt.failure_reason = item.message
            attempt.finished_at = timezone.now()
            attempt.save(update_fields=["status", "failure_reason", "finished_at"])
            _log_finished(attempt, user)
            return attempt
        _mark_reverted(item)
        successes += 1

    attempt.status = RevertStatus.REVERTED
    attempt.failure_reason = ""
    attempt.finished_at = timezone.now()
    attempt.save(update_fields=["status", "failure_reason", "finished_at"])
    _log_finished(attempt, user)
    return attempt


def _drift_reason(
    mutation: ImportMutation,
    current: dict[str, Any] | None,
    *,
    expected_values: dict[str, Any] | None = None,
    expected_last_updated: str = "",
) -> str:
    label = f"{mutation.object_type} {mutation.object_name or mutation.object_id}"
    if current is None:
        return f"{label} no longer exists."
    expected_values = expected_values or mutation.after_values
    expected_last_updated = expected_last_updated or mutation.post_last_updated
    held_last_updated = str(current.get("last_updated") or "")
    if not expected_last_updated:
        return f"{label} has no recorded post-write last_updated value."
    if held_last_updated != expected_last_updated:
        return f"{label} changed after apply (last_updated differs)."
    held_values = projected(current, expected_values)
    if held_values != expected_values:
        changed = ", ".join(
            field
            for field, expected in expected_values.items()
            if held_values.get(field) != expected
        )
        return f"{label} changed after apply ({changed or 'recorded fields differ'})."
    return ""


def _fingerprint_row(mutation: ImportMutation, current: dict[str, Any]) -> dict[str, Any]:
    inverse = (
        {"method": "delete", "fields": {}}
        if mutation.action == MutationAction.CREATE
        else {"method": "patch", "fields": canonical(mutation.before_values)}
    )
    return {
        "mutation": mutation.pk,
        "endpoint": mutation.endpoint,
        "object_id": mutation.object_id,
        "last_updated": str(current.get("last_updated") or ""),
        "inverse": inverse,
    }


def _fingerprint(
    *,
    run: ImportRun,
    user: Any,
    host: str,
    expires_at: Any,
    rows: list[dict[str, Any]],
) -> str:
    payload = json.dumps(
        {
            "run": run.pk,
            "user": user.pk,
            "host": host,
            "expires_at": expires_at.isoformat() if expires_at else "",
            "rows": rows,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return salted_hmac("importer.revert-plan", payload).hexdigest()


def _reconcile_inverse(mutation: ImportMutation, client: Any) -> bool | None:
    """True means inverse applied, False means still in post-apply state, None is ambiguous."""
    if mutation.object_id is None:
        return None
    current = client.get_one(mutation.endpoint, mutation.object_id)
    if mutation.action == MutationAction.CREATE:
        if current is None:
            return True
        return False if projected(current, mutation.after_values) == mutation.after_values else None
    if current is None:
        return None
    if projected(current, mutation.before_values) == mutation.before_values:
        return True
    if projected(current, mutation.after_values) == mutation.after_values:
        return False
    return None


def _mark_reverted(item: RevertItemResult) -> None:
    item.outcome = RevertItemOutcome.REVERTED
    item.message = "Deleted." if item.mutation.action == MutationAction.CREATE else "Restored."
    item.save(update_fields=["outcome", "message"])


def _expected_post_state(
    mutation: ImportMutation,
) -> tuple[dict[str, Any], str]:
    checkpoint = (
        mutation.revert_results.exclude(checkpoint_last_updated="")
        .order_by("-attempt__started_at", "-pk")
        .first()
    )
    if checkpoint is None:
        return dict(mutation.after_values), mutation.post_last_updated
    return dict(checkpoint.checkpoint_values), checkpoint.checkpoint_last_updated


def _checkpoint_items(items: list[RevertItemResult], client: Any) -> None:
    """Capture state after this attempt's own side effects for a safe retry."""
    for item in items:
        mutation = item.mutation
        if mutation.object_id is None:
            continue
        try:
            current = client.get_one(mutation.endpoint, mutation.object_id)
        except NetBoxUnavailable:
            continue
        if current is None:
            continue
        item.checkpoint_values = projected(current, mutation.after_values)
        item.checkpoint_last_updated = str(current.get("last_updated") or "")
        item.save(update_fields=["checkpoint_values", "checkpoint_last_updated"])


def _block(attempt: RevertAttempt, reason: str, *, preserve_items: bool = False) -> RevertAttempt:
    attempt.status = RevertStatus.PREFLIGHT_BLOCKED
    attempt.failure_reason = reason[:500]
    attempt.finished_at = timezone.now()
    attempt.save(update_fields=["status", "failure_reason", "finished_at"])
    if not preserve_items:
        attempt.items.all().delete()
    logger.warning(
        "revert preflight user=%s run=%s attempt=%s status=blocked reason=%s",
        getattr(attempt.user, "username", "-"),
        attempt.original_run_id,
        attempt.pk,
        reason,
    )
    return attempt


def _abort(attempt: RevertAttempt, reason: str) -> RevertAttempt:
    attempt.status = RevertStatus.ABORTED
    attempt.failure_reason = reason[:500]
    attempt.finished_at = timezone.now()
    attempt.save(update_fields=["status", "failure_reason", "finished_at"])
    _log_finished(attempt, attempt.user)
    return attempt


def _log_finished(attempt: RevertAttempt, user: Any) -> None:
    logger.info(
        "revert finished user=%s run=%s attempt=%s status=%s",
        getattr(user, "username", "-"),
        attempt.original_run_id,
        attempt.pk,
        attempt.status,
    )
