# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Durable per-object write intents and outcomes for history and safe reversion (D14)."""

from __future__ import annotations

from typing import Any

from django.db.models import Max
from django.utils import timezone

from importer.models import (
    ImportMutation,
    ImportRun,
    MutationAction,
    MutationOutcome,
)


def canonical(value: Any) -> Any:
    """Reduce NetBox brief objects and payload values to stable JSON-compatible values."""
    if isinstance(value, dict):
        if value.get("id") is not None:
            return int(value["id"])
        if value.get("value") is not None and set(value) <= {"value", "label"}:
            return value["value"]
        return {str(key): canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonical_field(field: str, value: Any) -> Any:
    result = canonical(value)
    if field == "tags":
        if result is None:
            return []
        if isinstance(result, list):
            return sorted(result, key=lambda item: str(item))
    return result


def projected(record: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    return {field: canonical_field(field, record.get(field)) for field in expected}


class MutationJournal:
    """Records intent before I/O, then finalises the same entries from NetBox's response."""

    def __init__(self, run: ImportRun) -> None:
        self.run = run
        highest = run.mutations.aggregate(value=Max("sequence"))["value"]
        self._next_sequence = int(highest or 0) + 1

    def intend_creates(
        self,
        endpoint: str,
        actions: list[Any],
        payloads: list[dict[str, Any]],
    ) -> list[ImportMutation]:
        mutations: list[ImportMutation] = []
        for action, payload in zip(actions, payloads, strict=False):
            mutations.append(
                ImportMutation(
                    run=self.run,
                    sequence=self._take_sequence(),
                    action=MutationAction.CREATE,
                    endpoint=endpoint,
                    source_lines=list(action.all_lines),
                    natural_key=canonical(action.key),
                    object_type=action.object_kind or endpoint,
                    object_name=action.label,
                    after_values=canonical(payload),
                    outcome=MutationOutcome.INTENT,
                )
            )
        return list(ImportMutation.objects.bulk_create(mutations))

    def intend_updates(
        self,
        endpoint: str,
        actions: list[Any],
        payloads: list[dict[str, Any]],
        client: Any,
    ) -> list[ImportMutation]:
        mutations: list[ImportMutation] = []
        for action, payload in zip(actions, payloads, strict=False):
            object_id = int(payload["id"])
            current = client.get_one(endpoint, object_id) or {}
            after = {
                field: canonical_field(field, value)
                for field, value in payload.items()
                if field != "id"
            }
            before = {field: canonical_field(field, current.get(field)) for field in after}
            mutations.append(
                ImportMutation(
                    run=self.run,
                    sequence=self._take_sequence(),
                    action=MutationAction.UPDATE,
                    endpoint=endpoint,
                    source_lines=list(action.all_lines),
                    natural_key=canonical(action.key),
                    object_type=action.object_kind or endpoint,
                    object_id=object_id,
                    object_name=action.label,
                    url=client.object_url(endpoint, object_id),
                    before_values=before,
                    after_values=after,
                    outcome=MutationOutcome.INTENT,
                )
            )
        return list(ImportMutation.objects.bulk_create(mutations))

    def applied(
        self,
        mutations: list[ImportMutation],
        records: list[dict[str, Any]],
        client: Any,
    ) -> None:
        for mutation, response_record in zip(mutations, records, strict=False):
            object_id = response_record.get("id") or mutation.object_id
            record = response_record
            if object_id is not None and not response_record.get("last_updated"):
                record = client.get_one(mutation.endpoint, int(object_id)) or response_record
            mutation.object_id = int(object_id) if object_id is not None else None
            mutation.object_name = str(
                record.get("display") or record.get("name") or mutation.object_name
            )
            mutation.url = (
                client.object_url(mutation.endpoint, mutation.object_id)
                if mutation.object_id is not None
                else mutation.url
            )
            mutation.after_values = projected(record, mutation.after_values)
            mutation.post_last_updated = str(record.get("last_updated") or "")
            mutation.outcome = MutationOutcome.APPLIED
            mutation.applied_at = timezone.now()
            mutation.error = ""
            mutation.save(
                update_fields=[
                    "object_id",
                    "object_name",
                    "url",
                    "after_values",
                    "post_last_updated",
                    "outcome",
                    "applied_at",
                    "error",
                ]
            )
        for mutation in mutations[len(records) :]:
            self.unknown([mutation], "NetBox returned no result for this operation.")

    @staticmethod
    def failed(mutations: list[ImportMutation], detail: str) -> None:
        ImportMutation.objects.filter(pk__in=[item.pk for item in mutations]).update(
            outcome=MutationOutcome.FAILED,
            error=detail[:500],
        )

    @staticmethod
    def unknown(mutations: list[ImportMutation], detail: str) -> None:
        ImportMutation.objects.filter(pk__in=[item.pk for item in mutations]).update(
            outcome=MutationOutcome.UNKNOWN,
            error=detail[:500],
        )

    def _take_sequence(self) -> int:
        sequence = self._next_sequence
        self._next_sequence += 1
        return sequence

    def refresh_post_state(self, client: Any) -> None:
        """Capture the final run state after NetBox's later-write side effects have settled."""
        for mutation in self.run.mutations.filter(
            outcome=MutationOutcome.APPLIED,
            object_id__isnull=False,
        ):
            current = client.get_one(mutation.endpoint, mutation.object_id)
            if current is None:
                continue
            mutation.post_last_updated = str(current.get("last_updated") or "")
            mutation.save(update_fields=["post_last_updated"])


def reconcile_unknown(run: ImportRun, client: Any) -> list[ImportMutation]:
    """Resolve unknown outcomes where current NetBox state proves one answer."""
    unresolved: list[ImportMutation] = []
    for mutation in run.mutations.filter(outcome=MutationOutcome.UNKNOWN):
        if mutation.object_id is not None:
            current = client.get_one(mutation.endpoint, mutation.object_id)
            if mutation.action == MutationAction.CREATE:
                if current is None:
                    _set_outcome(mutation, MutationOutcome.FAILED)
                elif projected(current, mutation.after_values) == mutation.after_values:
                    _mark_reconciled_applied(mutation, current, client)
                else:
                    unresolved.append(mutation)
                continue
            if current is None:
                unresolved.append(mutation)
            elif projected(current, mutation.after_values) == mutation.after_values:
                _mark_reconciled_applied(mutation, current, client)
            elif projected(current, mutation.before_values) == mutation.before_values:
                _set_outcome(mutation, MutationOutcome.FAILED)
            else:
                unresolved.append(mutation)
            continue

        matches = [
            record
            for record in client.get_all(mutation.endpoint)
            if projected(record, mutation.after_values) == mutation.after_values
        ]
        if len(matches) == 1:
            _mark_reconciled_applied(mutation, matches[0], client)
        elif not matches:
            _set_outcome(mutation, MutationOutcome.FAILED)
        else:
            unresolved.append(mutation)
    return unresolved


def _set_outcome(mutation: ImportMutation, outcome: str) -> None:
    mutation.outcome = outcome
    mutation.error = ""
    mutation.save(update_fields=["outcome", "error"])


def _mark_reconciled_applied(mutation: ImportMutation, record: dict[str, Any], client: Any) -> None:
    object_id = record.get("id") or mutation.object_id
    mutation.object_id = int(object_id) if object_id is not None else None
    mutation.url = (
        client.object_url(mutation.endpoint, mutation.object_id)
        if mutation.object_id is not None
        else mutation.url
    )
    mutation.object_name = str(record.get("display") or record.get("name") or mutation.object_name)
    mutation.after_values = projected(record, mutation.after_values)
    mutation.post_last_updated = str(record.get("last_updated") or "")
    mutation.outcome = MutationOutcome.APPLIED
    mutation.applied_at = timezone.now()
    mutation.error = ""
    mutation.save()
